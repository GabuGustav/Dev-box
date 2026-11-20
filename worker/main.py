import asyncio
import hashlib
import hmac
import logging
import os
import re
import threading
from contextlib import asynccontextmanager, contextmanager
from functools import wraps

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, status

from worker.snapshots import SnapshotConflict, SnapshotError, SnapshotNotFound, SnapshotStore
from worker.storage import WorkspaceStorageError, XFSWorkspaceStorage

logger = logging.getLogger("devbox.executor")
WORKSPACE_ID_PATTERN = re.compile(r"^dbx_[A-Za-z0-9_-]{20,40}$")
IMAGE_NAME = os.getenv("DEVBOX_WORKSPACE_IMAGE", "devbox-workspace:0.1")
WORKSPACE_CPU = float(os.getenv("DEVBOX_WORKSPACE_CPU", "1.0"))
WORKSPACE_MEMORY = os.getenv("DEVBOX_WORKSPACE_MEMORY", "1g")
WORKSPACE_PIDS = int(os.getenv("DEVBOX_WORKSPACE_PIDS", "256"))
SERVICE_TOKEN = os.getenv("DEVBOX_EXECUTOR_TOKEN", "")
WORKSPACE_IMAGE_CONTEXT = os.getenv(
    "DEVBOX_WORKSPACE_IMAGE_CONTEXT", "/opt/devbox/workspace-image"
)
WORKSPACE_STORAGE_ROOT = os.getenv("DEVBOX_WORKSPACE_STORAGE_ROOT", "/srv/devbox/workspaces")
WORKSPACE_DISK_QUOTA = os.getenv("DEVBOX_WORKSPACE_DISK_QUOTA", "10g")
WORKSPACE_INODE_LIMIT = int(os.getenv("DEVBOX_WORKSPACE_INODE_LIMIT", "200000"))
SNAPSHOT_STORAGE_ROOT = os.getenv("DEVBOX_SNAPSHOT_STORAGE_ROOT", "/srv/devbox/snapshots")
SNAPSHOT_DISK_QUOTA = os.getenv("DEVBOX_SNAPSHOT_DISK_QUOTA", "20g")
SNAPSHOT_INODE_LIMIT = int(os.getenv("DEVBOX_SNAPSHOT_INODE_LIMIT", "50000"))
SNAPSHOT_ENCRYPTION_KEY = os.getenv("DEVBOX_SNAPSHOT_ENCRYPTION_KEY", "")
SNAPSHOT_MAX_COUNT = int(os.getenv("DEVBOX_SNAPSHOT_MAX_COUNT", "10"))
SNAPSHOT_MAX_BYTES = int(os.getenv("DEVBOX_SNAPSHOT_MAX_BYTES", str(10 * 1024**3)))
RECONCILE_INTERVAL_SECONDS = int(os.getenv("DEVBOX_RECONCILE_INTERVAL_SECONDS", "60"))
workspace_storage = XFSWorkspaceStorage(
    WORKSPACE_STORAGE_ROOT, WORKSPACE_DISK_QUOTA, WORKSPACE_INODE_LIMIT
)
snapshot_storage = XFSWorkspaceStorage(
    SNAPSHOT_STORAGE_ROOT, SNAPSHOT_DISK_QUOTA, SNAPSHOT_INODE_LIMIT
)
snapshot_store = SnapshotStore(
    snapshot_storage,
    workspace_storage,
    SNAPSHOT_ENCRYPTION_KEY,
    max_snapshots=SNAPSHOT_MAX_COUNT,
    max_snapshot_bytes=SNAPSHOT_MAX_BYTES,
)
docker_client = docker.from_env()


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


def ensure_image() -> None:
    try:
        docker_client.images.get(IMAGE_NAME)
    except ImageNotFound:
        docker_client.images.build(path=WORKSPACE_IMAGE_CONTEXT, tag=IMAGE_NAME, rm=True)


def _stop_unsafe_container(container, reason: str) -> None:
    try:
        container.reload()
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status in {"running", "restarting"}:
            container.stop(timeout=10)
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
    """Stop managed containers whose workspace mount no longer matches its XFS directory."""
    stopped = 0
    try:
        containers = docker_client.containers.list(
            all=True, filters={"label": "devbox.managed=true"}
        )
    except APIError:
        logger.exception("Unable to enumerate managed workspace containers")
        return stopped

    for container in containers:
        try:
            container.reload()
            labels = container.attrs.get("Config", {}).get("Labels") or {}
            workspace_id = labels.get("devbox.workspace_id")
            if labels.get("devbox.managed") != "true" or not workspace_id:
                _stop_unsafe_container(container, "managed workspace labels are missing")
                stopped += 1
                continue
            with workspace_lock(workspace_id):
                container.reload()
                labels = container.attrs.get("Config", {}).get("Labels") or {}
                if labels.get("devbox.managed") != "true":
                    _stop_unsafe_container(container, "managed workspace label changed")
                    stopped += 1
                    continue
                expected_name, _ = names_for(workspace_id)
                if container.name != expected_name:
                    _stop_unsafe_container(container, "container name does not match workspace ID")
                    stopped += 1
                    continue
                snapshot_store.recover_restore(workspace_id)
                workspace_path = workspace_storage.ensure_workspace(workspace_id)
                mounts = container.attrs.get("Mounts", [])
                workspace_mount = next(
                    (mount for mount in mounts if mount.get("Destination") == "/workspace"),
                    None,
                )
                if (
                    workspace_mount is None
                    or workspace_mount.get("Source") != str(workspace_path)
                ):
                    _stop_unsafe_container(
                        container, "workspace bind mount does not match XFS storage"
                    )
                    stopped += 1
        except Exception as exc:
            _stop_unsafe_container(container, f"workspace storage validation failed: {exc}")
            stopped += 1
    if stopped:
        logger.warning(
            "Workspace reconciliation stopped unsafe containers",
            extra={"count": stopped},
        )
    return stopped


async def reconciliation_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(reconcile_managed_containers)
        except Exception:
            logger.exception("Workspace reconciliation loop failed")
        await asyncio.sleep(max(5, RECONCILE_INTERVAL_SECONDS))


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not SERVICE_TOKEN:
        logger.warning(
            "DEVBOX_EXECUTOR_TOKEN is not configured; internal calls will be rejected."
        )
    ensure_image()
    reconciler = asyncio.create_task(reconciliation_loop())
    try:
        yield
    finally:
        reconciler.cancel()
        await asyncio.gather(reconciler, return_exceptions=True)


app = FastAPI(title="DevBox Executor", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        docker_client.ping()
    except APIError as exc:
        raise HTTPException(status_code=503, detail="Docker daemon unavailable.") from exc
    return {"status": "ready"}


@app.post(
    "/internal/workspaces/{workspace_id}/start",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def start_workspace(workspace_id: str) -> dict:
    container_name, storage_name = names_for(workspace_id)
    try:
        snapshot_store.recover_restore(workspace_id)
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        mounts = container.attrs.get("Mounts", [])
        workspace_mount = next(
            (mount for mount in mounts if mount.get("Destination") == "/workspace"),
            None,
        )
        if workspace_mount is None or workspace_mount.get("Source") != str(workspace_path):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Existing workspace uses legacy or unexpected storage. "
                    "Migrate its data before starting it with the XFS backend."
                ),
            )
    except NotFound:
        container = docker_client.containers.create(
            image=IMAGE_NAME,
            name=container_name,
            command=["sleep", "infinity"],
            detach=True,
            user="10001:10001",
            working_dir="/workspace",
            environment={"HOME": "/workspace/.home", "TMPDIR": "/tmp"},
            network_mode="none",
            volumes={str(workspace_path): {"bind": "/workspace", "mode": "rw"}},
            mem_limit=WORKSPACE_MEMORY,
            nano_cpus=int(WORKSPACE_CPU * 1_000_000_000),
            pids_limit=WORKSPACE_PIDS,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            tmpfs={
                "/tmp": "rw,noexec,nosuid,size=64m",
                "/run": "rw,noexec,nosuid,size=16m",
            },
            init=True,
            restart_policy={"Name": "unless-stopped"},
            labels={"devbox.managed": "true", "devbox.workspace_id": workspace_id},
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


@app.post(
    "/internal/workspaces/{workspace_id}/stop",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def stop_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status == "running":
            container.stop(timeout=10)
        container.reload()
        return {
            "workspace_id": workspace_id,
            "container_id": container.id,
            "storage_ref": volume_name,
            "state": container.status,
        }
    except NotFound:
        return {
            "workspace_id": workspace_id,
            "container_id": None,
            "storage_ref": volume_name,
            "state": "absent",
        }


@app.post(
    "/internal/workspaces/{workspace_id}/pause",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def pause_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        if container.status == "running":
            container.pause()
        container.reload()
        return {
            "workspace_id": workspace_id,
            "container_id": container.id,
            "storage_ref": volume_name,
            "state": container.status,
        }
    except NotFound as exc:
        raise HTTPException(
            status_code=409,
            detail="Workspace executor does not exist.",
        ) from exc


@app.post(
    "/internal/workspaces/{workspace_id}/resume",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def resume_workspace(workspace_id: str) -> dict:
    container_name, storage_name = names_for(workspace_id)
    try:
        snapshot_store.recover_restore(workspace_id)
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
    except (WorkspaceStorageError, SnapshotError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        mounts = container.attrs.get("Mounts", [])
        workspace_mount = next(
            (mount for mount in mounts if mount.get("Destination") == "/workspace"),
            None,
        )
        if workspace_mount is None or workspace_mount.get("Source") != str(workspace_path):
            raise HTTPException(
                status_code=409,
                detail="Existing workspace is not attached to its assigned XFS storage.",
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
    except NotFound as exc:
        raise HTTPException(
            status_code=409,
            detail="Workspace executor does not exist.",
        ) from exc


@app.post(
    "/internal/workspaces/{workspace_id}/snapshots",
    dependencies=[Depends(require_service_token)],
)
@serialize_workspace_operation
def create_snapshot(workspace_id: str) -> dict:
    store = require_snapshot_store()
    container_name, _ = names_for(workspace_id)
    try:
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
        was_running = False
        try:
            container = docker_client.containers.get(container_name)
            container.reload()
            mounts = container.attrs.get("Mounts", [])
            workspace_mount = next(
                (mount for mount in mounts if mount.get("Destination") == "/workspace"),
                None,
            )
            if workspace_mount is None or workspace_mount.get("Source") != str(workspace_path):
                raise HTTPException(
                    status_code=409,
                    detail="Workspace container is not attached to its assigned storage.",
                )
            if container.status == "running":
                container.pause()
                was_running = True
            elif container.status == "paused":
                pass
            elif container.status not in {"created", "exited"}:
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot snapshot a workspace in state {container.status}.",
                )
        except NotFound:
            container = None
        try:
            snapshot = store.create(workspace_id, workspace_path)
        finally:
            if was_running and container is not None:
                container.unpause()
        return snapshot
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
    container_name, _ = names_for(workspace_id)
    try:
        store.recover_restore(workspace_id)
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
        try:
            container = docker_client.containers.get(container_name)
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
                raise HTTPException(
                    status_code=409,
                    detail="Workspace container is not attached to its assigned storage.",
                )
            container_id = container.id
        except NotFound:
            container_id = None
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
    container_name, volume_name = names_for(workspace_id)
    try:
        workspace_storage.validate_backend()
    except WorkspaceStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        container = docker_client.containers.get(container_name)
        container.remove(force=True, v=True)
    except NotFound:
        pass
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
