import asyncio
import hashlib
import hmac
import logging
import os
import re
import threading
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from worker.broker_client import (
    BrokerClient,
    BrokerError,
    BrokerNotFound,
    BrokerRejected,
    BrokerUnavailable,
)
from worker.snapshots import SnapshotConflict, SnapshotError, SnapshotNotFound, SnapshotStore
from worker.remote_storage import RemoteWorkspaceStorage
from worker.storage import WorkspaceStorageError

logger = logging.getLogger("devbox.executor")
WORKSPACE_ID_PATTERN = re.compile(r"^dbx_[A-Za-z0-9_-]{20,40}$")
GIT_ALLOWED_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
SERVICE_TOKEN = os.getenv("DEVBOX_EXECUTOR_TOKEN", "")
WORKSPACE_STORAGE_ROOT = os.getenv("DEVBOX_WORKSPACE_STORAGE_ROOT") or "/srv/devbox/workspaces"
WORKSPACE_DISK_QUOTA = os.getenv("DEVBOX_WORKSPACE_DISK_QUOTA", "10g")
WORKSPACE_INODE_LIMIT = int(os.getenv("DEVBOX_WORKSPACE_INODE_LIMIT", "200000"))
SNAPSHOT_STORAGE_ROOT = os.getenv("DEVBOX_SNAPSHOT_STORAGE_ROOT") or "/srv/devbox/snapshots"
SNAPSHOT_DISK_QUOTA = os.getenv("DEVBOX_SNAPSHOT_DISK_QUOTA", "20g")
SNAPSHOT_INODE_LIMIT = int(os.getenv("DEVBOX_SNAPSHOT_INODE_LIMIT", "50000"))
SNAPSHOT_ENCRYPTION_KEY = os.getenv("DEVBOX_SNAPSHOT_ENCRYPTION_KEY", "")
SNAPSHOT_MAX_COUNT = int(os.getenv("DEVBOX_SNAPSHOT_MAX_COUNT", "10"))
SNAPSHOT_MAX_BYTES = int(os.getenv("DEVBOX_SNAPSHOT_MAX_BYTES", str(10 * 1024**3)))
RECONCILE_INTERVAL_SECONDS = int(os.getenv("DEVBOX_RECONCILE_INTERVAL_SECONDS", "60"))
broker_client = BrokerClient()
workspace_storage = RemoteWorkspaceStorage(
    WORKSPACE_STORAGE_ROOT,
    WORKSPACE_QUOTA,
    WORKSPACE_INODE_LIMIT,
    "workspace",
    broker_client,
)
snapshot_storage = RemoteWorkspaceStorage(
    SNAPSHOT_STORAGE_ROOT,
    SNAPSHOT_QUOTA,
    SNAPSHOT_INODE_LIMIT,
    "snapshot",
    broker_client,
)
snapshot_store = SnapshotStore(
    snapshot_storage,
    workspace_storage,
    SNAPSHOT_ENCRYPTION_KEY,
    max_snapshots=SNAPSHOT_MAX_COUNT,
    max_snapshot_bytes=SNAPSHOT_MAX_BYTES,
)


class GitCloneRequest(BaseModel):
    repository_url: str = Field(min_length=8, max_length=500)
    branch: str = Field(min_length=1, max_length=255)


class GitBranchRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=255)


class GitRepositoryRequest(BaseModel):
    repository_url: str = Field(min_length=8, max_length=500)


class GitCommitRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    paths: list[str] = Field(default_factory=list, max_length=100)


def validate_git_repository_url(raw_url: str) -> str:
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Repository URL is invalid.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in GIT_ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Git operations currently support public HTTPS repositories on "
                "GitHub, GitLab, and Bitbucket."
            ),
        )
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise HTTPException(
            status_code=422,
            detail="Repository URL must identify an owner and repository.",
        )
    repository = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if not parts[0] or not repository:
        raise HTTPException(status_code=422, detail="Repository URL is invalid.")
    return f"https://{parsed.hostname.lower()}/{parts[0]}/{repository}"


def validate_git_branch(branch: str) -> str:
    invalid = (
        not branch
        or len(branch) > 255
        or branch.startswith("-")
        or branch.startswith("/")
        or branch.endswith(("/", "."))
        or "//" in branch
        or ".." in branch
        or "@{" in branch
        or any(ord(char) < 32 or char in " ~^:?*[]\\" for char in branch)
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    )
    if invalid:
        raise HTTPException(status_code=422, detail="Git branch name is invalid.")
    return branch


def ensure_git_workspace(workspace_id: str) -> Path:
    try:
        return workspace_storage.ensure_workspace(workspace_id)
    except HTTPException:
        raise
    except WorkspaceStorageError as exc:
        raise HTTPException(
            status_code=503, detail="Workspace storage is unavailable."
        ) from exc


def run_git_command(
    workspace_id: str,
    arguments: list[str],
    *,
    network_enabled: bool = False,
) -> str:
    ensure_git_workspace(workspace_id)
    try:
        return broker_client.run_git_helper(
            workspace_id,
            arguments,
            network_enabled=network_enabled,
        )
    except BrokerRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except BrokerUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Privileged broker is unavailable.",
        ) from exc


def require_snapshot_store() -> SnapshotStore:
    return snapshot_store


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {SERVICE_TOKEN}"
    if (
        not SERVICE_TOKEN
        or not authorization
        or not hmac.compare_digest(authorization, expected)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def broker_call(operation):
    try:
        return operation()
    except BrokerRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except BrokerUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Privileged broker is unavailable.",
        ) from exc


def names_for(workspace_id: str) -> tuple[str, str]:
    if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise HTTPException(status_code=422, detail="Invalid workspace ID.")
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
    return f"devbox-ws-{digest}", f"xfs-{digest}"


_workspace_locks: dict[str, threading.RLock] = {}
_workspace_locks_guard = threading.Lock()


@contextmanager
def workspace_lock(workspace_id: str):
    names_for(workspace_id)
    with _workspace_locks_guard:
        lock = _workspace_locks.setdefault(workspace_id, threading.RLock())
    with lock:
        yield


def serialize_workspace_operation(function):
    @wraps(function)
    def wrapped(workspace_id: str, *args, **kwargs):
        with workspace_lock(workspace_id):
            return function(workspace_id, *args, **kwargs)

    return wrapped


def _stop_unsafe_container(container, reason: str) -> None:
    try:
        container.reload()
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status in {"running", "restarting"}:
            container.stop(timeout=0)
        logger.error(
            "Stopped workspace container after storage validation failed",
            extra={
                "container_id": container.id,
                "container_name": container.name,
                "reason": reason,
            },
        )
    except Exception:
        logger.exception(
            "Failed to stop an unsafe workspace container",
            extra={"container_id": getattr(container, "id", None), "reason": reason},
        )


def reconcile_managed_containers() -> int:
    """Recover snapshot transactions for broker-managed workspaces."""
    recovered = 0
    try:
        workspace_ids = broker_client.list_managed_workspace_ids()
    except BrokerUnavailable:
        logger.exception("Unable to enumerate broker-managed workspaces")
        return recovered

    for workspace_id in workspace_ids:
        try:
            with workspace_lock(workspace_id):
                snapshot_store.recover_restore(workspace_id)
                workspace_storage.validate_backend()
                recovered += 1
        except Exception:
            logger.exception(
                "Workspace recovery failed; asking broker to stop executor",
                extra={"workspace_id": workspace_id},
            )
            try:
                broker_client.stop_workspace(workspace_id)
            except BrokerError:
                logger.exception(
                    "Broker could not stop workspace after recovery failure",
                    extra={"workspace_id": workspace_id},
                )
    return recovered


def reconcile_orphaned_git_helpers() -> int:
    """Delegate one-shot Git helper cleanup to the broker."""
    try:
        return broker_client.reconcile_git_helpers()
    except BrokerUnavailable:
        logger.exception("Unable to reconcile temporary Git helpers through broker")
        return 0


async def reconciliation_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(reconcile_managed_containers)
            await asyncio.to_thread(reconcile_orphaned_git_helpers)
        except Exception:
            logger.exception("Workspace reconciliation loop failed")
        await asyncio.sleep(max(5, RECONCILE_INTERVAL_SECONDS))


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not SERVICE_TOKEN:
        logger.warning(
            "DEVBOX_EXECUTOR_TOKEN is not configured; internal calls will be rejected."
        )
    broker_client.validate_config()
    try:
        broker_client.validate_connection()
    except BrokerError as exc:
        raise RuntimeError(
            "Privileged broker authentication/readiness check failed."
        ) from exc
    reconciler = asyncio.create_task(reconciliation_loop())
    try:
        yield
    finally:
        reconciler.cancel()
        await asyncio.gather(reconciler, return_exceptions=True)
        broker_client.close()


app = FastAPI(title="DevBox Executor", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        broker_client.validate_connection()
    except BrokerError as exc:
        raise HTTPException(
            status_code=503,
            detail="Privileged broker unavailable or unauthorized.",
        ) from exc
    return {"status": "ready"}


@app.post(
    "/internal/workspaces/{workspace_id}/start",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def start_workspace(workspace_id: str) -> dict:
    _, storage_name = names_for(workspace_id)
    try:
        snapshot_store.recover_restore(workspace_id)
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        try:
            container = broker_call(lambda: broker_client.get_workspace(workspace_id))
        except BrokerNotFound:
            container = broker_call(lambda: broker_client.start_workspace(workspace_id))
        container.reload()
        mounts = container.attrs.get("Mounts", [])
        workspace_mount = next(
            (mount for mount in mounts if mount.get("Destination") == "/workspace"),
            None,
        )
        if workspace_mount is None or workspace_mount.get("Source") != str(workspace_path):
            _stop_unsafe_container(container, "broker returned an unexpected workspace mount")
            raise HTTPException(
                status_code=409,
                detail="Workspace container is not attached to its assigned storage.",
            )
        if container.status == "paused":
            container.unpause()
        elif container.status != "running":
            container.start()
        container.reload()
        return {
            "workspace_id": workspace_id,
            "container_id": container.id,
            "storage_ref": storage_name,
            "state": container.status,
        }
    except BrokerNotFound as exc:
        raise HTTPException(status_code=409, detail="Workspace executor is absent.") from exc


@app.post(
    "/internal/workspaces/{workspace_id}/stop",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def stop_workspace(workspace_id: str) -> dict:
    names_for(workspace_id)
    return broker_call(lambda: broker_client.stop_workspace(workspace_id))


@app.post(
    "/internal/workspaces/{workspace_id}/pause",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def pause_workspace(workspace_id: str) -> dict:
    names_for(workspace_id)
    return broker_call(lambda: broker_client.pause_workspace(workspace_id))


@app.post(
    "/internal/workspaces/{workspace_id}/resume",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def resume_workspace(workspace_id: str) -> dict:
    names_for(workspace_id)
    try:
        snapshot_store.recover_restore(workspace_id)
        workspace_storage.ensure_workspace(workspace_id)
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return broker_call(lambda: broker_client.resume_workspace(workspace_id))


@app.post(
    "/internal/workspaces/{workspace_id}/snapshots",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def create_snapshot(workspace_id: str) -> dict:
    store = require_snapshot_store()
    names_for(workspace_id)
    try:
        store.recover_restore(workspace_id)
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
        was_running = False
        try:
            container = broker_call(lambda: broker_client.get_workspace(workspace_id))
        except BrokerNotFound:
            container = None
        if container is not None:
            container.reload()
            mounts = container.attrs.get("Mounts", [])
            workspace_mount = next(
                (mount for mount in mounts if mount.get("Destination") == "/workspace"),
                None,
            )
            if workspace_mount is None or workspace_mount.get("Source") != str(workspace_path):
                _stop_unsafe_container(container, "workspace container mount mismatch")
                raise HTTPException(
                    status_code=409,
                    detail="Workspace container is not attached to its assigned storage.",
                )
            if container.status == "running":
                container.pause()
                was_running = True
            elif container.status != "paused" and container.status not in {"created", "exited"}:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot snapshot a workspace in state {container.status}.",
                )
        try:
            return store.create(workspace_id, workspace_path)
        finally:
            if was_running and container is not None:
                container.unpause()
    except SnapshotConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/internal/workspaces/{workspace_id}/snapshots",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def list_snapshots(workspace_id: str) -> dict:
    store = require_snapshot_store()
    try:
        return {"workspace_id": workspace_id, "items": store.list(workspace_id)}
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post(
    "/internal/workspaces/{workspace_id}/snapshots/{snapshot_id}/restore",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def restore_snapshot(workspace_id: str, snapshot_id: str) -> dict:
    store = require_snapshot_store()
    names_for(workspace_id)
    try:
        store.recover_restore(workspace_id)
        workspace_storage.validate_backend()
        _, workspace_path = workspace_storage._workspace_path(workspace_id)
        if workspace_path.is_symlink() or (
            workspace_path.exists() and not workspace_path.is_dir()
        ):
            raise HTTPException(
                status_code=409,
                detail="Workspace storage path is not a safe directory for restore.",
            )
        try:
            container = broker_call(lambda: broker_client.get_workspace(workspace_id))
        except BrokerNotFound:
            container = None
        container_id = None
        if container is not None:
            container.reload()
            if container.status in {"running", "paused"}:
                raise HTTPException(
                    status_code=409,
                    detail="Stop the workspace before restoring a snapshot.",
                )
            mounts = container.attrs.get("Mounts", [])
            workspace_mount = next(
                (mount for mount in mounts if mount.get("Destination") == "/workspace"),
                None,
            )
            if workspace_mount is None or workspace_mount.get("Source") != str(workspace_path):
                _stop_unsafe_container(container, "workspace container mount mismatch")
                raise HTTPException(
                    status_code=409,
                    detail="Workspace container is not attached to its assigned storage.",
                )
            container_id = container.id
        result = store.restore(workspace_id, snapshot_id, workspace_path)
        result["container_id"] = container_id
        result["storage_ref"] = names_for(workspace_id)[1]
        return result
    except SnapshotNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete(
    "/internal/workspaces/{workspace_id}/snapshots/{snapshot_id}",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def delete_snapshot(workspace_id: str, snapshot_id: str) -> dict:
    store = require_snapshot_store()
    try:
        store.delete(workspace_id, snapshot_id)
        return {"workspace_id": workspace_id, "snapshot_id": snapshot_id, "state": "deleted"}
    except SnapshotNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete(
    "/internal/workspaces/{workspace_id}",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def delete_workspace(workspace_id: str) -> dict:
    _, volume_name = names_for(workspace_id)
    try:
        snapshot_store.recover_restore(workspace_id)
        _, workspace_path = workspace_storage._workspace_path(workspace_id)
        if workspace_path.exists() or workspace_path.is_symlink():
            workspace_storage.ensure_workspace(workspace_id)
        else:
            workspace_storage.validate_backend()
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    broker_call(lambda: broker_client.delete_workspace(workspace_id))
    try:
        snapshot_store.delete_workspace(workspace_id)
        workspace_storage.delete_workspace(workspace_id)
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "workspace_id": workspace_id,
        "container_id": None,
        "storage_ref": volume_name,
        "state": "deleted",
    }


@app.post(
    "/internal/workspaces/{workspace_id}/git/clone",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def clone_workspace_repository(workspace_id: str, payload: GitCloneRequest) -> dict:
    repository_url = validate_git_repository_url(payload.repository_url)
    branch = validate_git_branch(payload.branch)
    workspace_path = ensure_git_workspace(workspace_id)
    if (workspace_path / ".git").is_dir():
        existing_url = validate_git_repository_url(
            run_git_command(workspace_id, ["remote", "get-url", "origin"])
        )
        existing_branch = run_git_command(
            workspace_id, ["branch", "--show-current"]
        )
        if existing_url == repository_url and existing_branch == branch:
            return {
                "workspace_id": workspace_id,
                "operation": "clone",
                "repository_url": repository_url,
                "branch": branch,
                "output": "Repository already cloned.",
            }
        raise HTTPException(
            status_code=409,
            detail="Workspace already contains a different Git checkout.",
        )
    if any(workspace_path.iterdir()):
        raise HTTPException(
            status_code=409,
            detail="Workspace directory is not empty; refusing to overwrite existing files.",
        )
    run_git_command(workspace_id, ["check-ref-format", "--branch", branch])
    output = run_git_command(
        workspace_id,
        ["clone", "--depth", "1", "--single-branch", "--branch", branch,
         "--", repository_url, "/workspace"],
        network_enabled=True,
    )
    return {
        "workspace_id": workspace_id,
        "operation": "clone",
        "repository_url": repository_url,
        "branch": branch,
        "output": output,
    }


@app.get(
    "/internal/workspaces/{workspace_id}/git/status",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def git_status(workspace_id: str) -> dict:
    workspace_path = ensure_git_workspace(workspace_id)
    if not (workspace_path / ".git").exists():
        raise HTTPException(status_code=409, detail="Workspace has not been cloned.")
    output = run_git_command(workspace_id, ["status", "--short", "--branch"])
    return {"workspace_id": workspace_id, "operation": "status", "output": output}


@app.get(
    "/internal/workspaces/{workspace_id}/git/diff",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def git_diff(workspace_id: str) -> dict:
    workspace_path = ensure_git_workspace(workspace_id)
    if not (workspace_path / ".git").exists():
        raise HTTPException(status_code=409, detail="Workspace has not been cloned.")
    output = run_git_command(
        workspace_id, ["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color"]
    )
    return {"workspace_id": workspace_id, "operation": "diff", "output": output}



@app.post(
    "/internal/workspaces/{workspace_id}/git/branches",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def git_create_branch(workspace_id: str, payload: GitBranchRequest) -> dict:
    workspace_path = ensure_git_workspace(workspace_id)
    if not (workspace_path / ".git").exists():
        raise HTTPException(status_code=409, detail="Workspace has not been cloned.")
    branch = validate_git_branch(payload.branch)
    run_git_command(workspace_id, ["check-ref-format", "--branch", branch])
    output = run_git_command(workspace_id, ["switch", "--create", branch])
    return {
        "workspace_id": workspace_id,
        "operation": "branch",
        "branch": branch,
        "output": output,
    }


@app.post(
    "/internal/workspaces/{workspace_id}/git/commit",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def git_commit(workspace_id: str, payload: GitCommitRequest) -> dict:
    if not payload.message.strip():
        raise HTTPException(status_code=422, detail="Commit message cannot be blank.")
    workspace_path = ensure_git_workspace(workspace_id)
    if not (workspace_path / ".git").exists():
        raise HTTPException(status_code=409, detail="Workspace has not been cloned.")
    if payload.paths:
        for path in payload.paths:
            if (
                not path
                or len(path) > 500
                or path.startswith("/")
                or path.startswith("-")
                or "\\" in path
                or any(part == ".." for part in path.split("/"))
                or any(ord(char) < 32 for char in path)
            ):
                raise HTTPException(status_code=422, detail="Commit path is invalid.")
        run_git_command(workspace_id, ["add", "--", *payload.paths])
    output = run_git_command(
        workspace_id,
        [
            "-c",
            "user.name=DevBox Agent",
            "-c",
            "user.email=devbox@localhost",
            "commit",
            "-m",
            payload.message,
        ],
    )
    commit = run_git_command(workspace_id, ["rev-parse", "HEAD"])
    return {
        "workspace_id": workspace_id,
        "operation": "commit",
        "commit": commit,
        "output": output,
    }


@app.post(
    "/internal/workspaces/{workspace_id}/git/fetch",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def git_fetch(workspace_id: str, payload: GitRepositoryRequest) -> dict:
    workspace_path = ensure_git_workspace(workspace_id)
    if not (workspace_path / ".git").exists():
        raise HTTPException(status_code=409, detail="Workspace has not been cloned.")
    trusted_url = validate_git_repository_url(payload.repository_url)
    run_git_command(workspace_id, ["remote", "set-url", "origin", trusted_url])
    output = run_git_command(
        workspace_id, ["fetch", "--prune", "origin"], network_enabled=True
    )
    return {"workspace_id": workspace_id, "operation": "fetch", "output": output}


@app.get(
    "/internal/workspaces/{workspace_id}/status",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def get_workspace_status(workspace_id: str) -> dict:
    """Return executor state after broker-side storage/container validation."""
    _, storage_name = names_for(workspace_id)
    try:
        snapshot_store.recover_restore(workspace_id)
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        container = broker_call(lambda: broker_client.get_workspace(workspace_id))
    except BrokerNotFound:
        return {
            "workspace_id": workspace_id,
            "container_id": None,
            "storage_ref": storage_name,
            "state": "absent",
        }
    except HTTPException as exc:
        if exc.status_code == 409:
            return {
                "workspace_id": workspace_id,
                "container_id": None,
                "storage_ref": storage_name,
                "state": "failed",
            }
        raise

    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    mounts = container.attrs.get("Mounts", [])
    workspace_mount = next(
        (mount for mount in mounts if mount.get("Destination") == "/workspace"),
        None,
    )
    if (
        labels.get("devbox.managed") != "true"
        or labels.get("devbox.workspace_id") != workspace_id
        or container.name != names_for(workspace_id)[0]
        or workspace_mount is None
        or workspace_mount.get("Source") != str(workspace_path)
    ):
        _stop_unsafe_container(
            container, "workspace identity or storage binding failed validation"
        )
        return {
            "workspace_id": workspace_id,
            "container_id": None,
            "storage_ref": storage_name,
            "state": "failed",
        }

    state = container.status
    if state not in {"created", "running", "paused", "restarting", "exited", "dead"}:
        state = "failed"
    return {
        "workspace_id": workspace_id,
        "container_id": container.id,
        "storage_ref": storage_name,
        "state": state,
    }
