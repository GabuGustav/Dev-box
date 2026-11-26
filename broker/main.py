from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("devbox.broker")

WORKSPACE_ID_PATTERN = re.compile(r"^dbx_[A-Za-z0-9_-]{20,40}$")
SERVICE_TOKEN = os.getenv("DEVBOX_BROKER_TOKEN", "")
IMAGE_NAME = os.getenv("DEVBOX_WORKSPACE_IMAGE", "devbox-workspace:0.2")
IMAGE_CONTEXT = os.getenv("DEVBOX_WORKSPACE_IMAGE_CONTEXT", "/opt/devbox/workspace-image")
WORKSPACE_STORAGE_ROOT = Path(
    os.getenv("DEVBOX_WORKSPACE_STORAGE_ROOT", "/srv/devbox/workspaces")
)
WORKSPACE_CPU = float(os.getenv("DEVBOX_WORKSPACE_CPU", "1.0"))
WORKSPACE_MEMORY = os.getenv("DEVBOX_WORKSPACE_MEMORY", "1g")
WORKSPACE_PIDS = int(os.getenv("DEVBOX_WORKSPACE_PIDS", "256"))
GIT_EGRESS_NETWORK = os.getenv("DEVBOX_GIT_EGRESS_NETWORK", "devbox_egress_internal")
GIT_PROXY_URL = os.getenv("DEVBOX_GIT_PROXY_URL", "http://egress-proxy:3128")
GIT_OPERATION_TIMEOUT_SECONDS = max(
    10, min(600, int(os.getenv("DEVBOX_GIT_OPERATION_TIMEOUT_SECONDS", "300")))
)
GIT_OUTPUT_LIMIT_BYTES = 64 * 1024
GIT_ALLOWED_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}

docker_client = docker.from_env(timeout=GIT_OPERATION_TIMEOUT_SECONDS + 30)


class GitHelperRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: list[str] = Field(min_length=1, max_length=110)
    network_enabled: bool = False


def validate_service_token() -> None:
    if len(SERVICE_TOKEN) < 32 or "replace-this-with" in SERVICE_TOKEN.lower():
        raise RuntimeError(
            "DEVBOX_BROKER_TOKEN must be a unique non-placeholder secret of at least 32 characters."
        )


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {SERVICE_TOKEN}"
    if (
        not SERVICE_TOKEN
        or not authorization
        or not hmac.compare_digest(authorization, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


def names_for(workspace_id: str) -> tuple[str, str]:
    if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise HTTPException(status_code=422, detail="Invalid workspace ID.")
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
    return f"devbox-ws-{digest}", f"xfs-{digest}"


def workspace_path_for(workspace_id: str) -> Path:
    names_for(workspace_id)
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
    path = WORKSPACE_STORAGE_ROOT / digest
    if (
        WORKSPACE_STORAGE_ROOT.is_symlink()
        or path.is_symlink()
        or not path.exists()
        or not path.is_dir()
        or path.resolve().parent != WORKSPACE_STORAGE_ROOT.resolve()
    ):
        raise HTTPException(
            status_code=409,
            detail="Workspace storage directory is missing or unsafe.",
        )
    return path


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
            detail="Git helper supports public HTTPS repositories on approved hosts only.",
        )
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2 or any(part in {".", ".."} for part in parts):
        raise HTTPException(status_code=422, detail="Repository URL is invalid.")
    repository = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if not parts[0] or not repository:
        raise HTTPException(status_code=422, detail="Repository URL is invalid.")
    return f"https://{parsed.hostname.lower()}/{parts[0]}/{repository}"


def validate_git_branch(branch: str) -> str:
    invalid = (
        not branch
        or len(branch) > 255
        or branch.startswith(("-", "/"))
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


def validate_git_path(path: str) -> str:
    if (
        not path
        or len(path) > 500
        or path.startswith(("/", "-"))
        or "\\" in path
        or any(part == ".." for part in path.split("/"))
        or any(ord(char) < 32 for char in path)
    ):
        raise HTTPException(status_code=422, detail="Git commit path is invalid.")
    return path


def validate_git_arguments(arguments: list[str], network_enabled: bool) -> None:
    if any(not isinstance(arg, str) or len(arg) > 1000 or "\x00" in arg for arg in arguments):
        raise HTTPException(status_code=422, detail="Git helper arguments are invalid.")

    command = arguments
    requires_network = False

    if command in (
        ["remote", "get-url", "origin"],
        ["branch", "--show-current"],
        ["status", "--short", "--branch"],
        ["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color"],
        ["rev-parse", "HEAD"],
        ["fetch", "--prune", "origin"],
    ):
        requires_network = command == ["fetch", "--prune", "origin"]
    elif len(command) == 3 and command[:2] == ["check-ref-format", "--branch"]:
        validate_git_branch(command[2])
    elif len(command) == 3 and command[:2] == ["switch", "--create"]:
        validate_git_branch(command[2])
    elif (
        len(command) == 9
        and command[:5] == ["clone", "--depth", "1", "--single-branch", "--branch"]
        and command[6] == "--"
        and command[8] == "/workspace"
    ):
        validate_git_branch(command[5])
        normalized = validate_git_repository_url(command[7])
        if normalized != command[7]:
            raise HTTPException(status_code=422, detail="Repository URL is not canonical.")
        requires_network = True
    elif len(command) >= 3 and command[:2] == ["add", "--"]:
        for path in command[2:]:
            validate_git_path(path)
    elif len(command) == 6 and command[:4] == [
        "-c", "user.name=DevBox Agent", "-c", "user.email=devbox@localhost"
    ] and command[4] == "commit" and command[5] == "-m":
        raise HTTPException(status_code=422, detail="Git commit message is missing.")
    elif (
        len(command) == 7
        and command[:4]
        == ["-c", "user.name=DevBox Agent", "-c", "user.email=devbox@localhost"]
        and command[4:6] == ["commit", "-m"]
    ):
        if not command[6].strip() or len(command[6]) > 1000:
            raise HTTPException(status_code=422, detail="Git commit message is invalid.")
    elif len(command) == 4 and command[:3] == ["remote", "set-url", "origin"]:
        normalized = validate_git_repository_url(command[3])
        if normalized != command[3]:
            raise HTTPException(status_code=422, detail="Repository URL is not canonical.")
    else:
        raise HTTPException(status_code=422, detail="Git helper operation is not allowlisted.")

    if network_enabled != requires_network:
        raise HTTPException(
            status_code=422,
            detail="Network access is only allowed for approved clone/fetch operations.",
        )


def ensure_image() -> None:
    try:
        docker_client.images.get(IMAGE_NAME)
    except ImageNotFound:
        docker_client.images.build(path=IMAGE_CONTEXT, tag=IMAGE_NAME, rm=True)


def _stop_unsafe_container(container, reason: str) -> None:
    try:
        container.reload()
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status in {"running", "restarting"}:
            container.stop(timeout=0)
        logger.error(
            "Stopped unsafe managed container",
            extra={
                "container_id": container.id,
                "container_name": container.name,
                "reason": reason,
            },
        )
    except Exception:
        logger.exception(
            "Failed to stop unsafe managed container",
            extra={"container_id": getattr(container, "id", None), "reason": reason},
        )


def _container_identity_is_valid(container, workspace_id: str, expected_path: Path) -> bool:
    expected_name, _ = names_for(workspace_id)
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    mounts = container.attrs.get("Mounts", [])
    mount = next((item for item in mounts if item.get("Destination") == "/workspace"), None)
    return (
        labels.get("devbox.managed") == "true"
        and labels.get("devbox.workspace_id") == workspace_id
        and container.name == expected_name
        and mount is not None
        and mount.get("Source") == str(expected_path)
    )


def _workspace_result(workspace_id: str, container=None) -> dict:
    _, storage_ref = names_for(workspace_id)
    return {
        "workspace_id": workspace_id,
        "container_id": container.id if container else None,
        "storage_ref": storage_ref,
        "state": container.status if container else "absent",
    }


def start_workspace(workspace_id: str) -> dict:
    container_name, _ = names_for(workspace_id)
    path = workspace_path_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        if not _container_identity_is_valid(container, workspace_id, path):
            _stop_unsafe_container(container, "workspace identity or storage binding mismatch")
            raise HTTPException(status_code=409, detail="Existing workspace container is unsafe.")
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
            ipc_mode="private",
            volumes={str(path): {"bind": "/workspace", "mode": "rw"},
            },
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
    return _workspace_result(workspace_id, container)


def stop_workspace(workspace_id: str) -> dict:
    container_name, _ = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
    except NotFound:
        return _workspace_result(workspace_id)
    try:
        path = workspace_path_for(workspace_id)
        container.reload()
        if not _container_identity_is_valid(container, workspace_id, path):
            _stop_unsafe_container(container, "workspace identity or storage binding mismatch")
            raise HTTPException(status_code=409, detail="Workspace container is unsafe.")
        if container.status == "paused":
            container.unpause()
            container.reload()
        if container.status == "running":
            container.stop(timeout=10)
        container.reload()
        return _workspace_result(workspace_id, container)
    except HTTPException:
        _stop_unsafe_container(container, "workspace storage path is missing or unsafe")
        raise


def pause_workspace(workspace_id: str) -> dict:
    container_name, _ = names_for(workspace_id)
    path = workspace_path_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        if not _container_identity_is_valid(container, workspace_id, path):
            _stop_unsafe_container(container, "workspace identity or storage binding mismatch")
            raise HTTPException(status_code=409, detail="Workspace container is unsafe.")
        if container.status == "running":
            container.pause()
        container.reload()
        return _workspace_result(workspace_id, container)
    except NotFound as exc:
        raise HTTPException(status_code=409, detail="Workspace executor does not exist.") from exc


def resume_workspace(workspace_id: str) -> dict:
    container_name, _ = names_for(workspace_id)
    path = workspace_path_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
    except NotFound as exc:
        raise HTTPException(status_code=409, detail="Workspace executor does not exist.") from exc
    if not _container_identity_is_valid(container, workspace_id, path):
        _stop_unsafe_container(container, "workspace identity or storage binding mismatch")
        raise HTTPException(status_code=409, detail="Workspace container is unsafe.")
    if container.status == "paused":
        container.unpause()
    elif container.status != "running":
        container.start()
    container.reload()
    return _workspace_result(workspace_id, container)


def inspect_workspace(workspace_id: str) -> dict:
    container_name, _ = names_for(workspace_id)
    path = workspace_path_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
    except NotFound:
        return _workspace_result(workspace_id)
    if not _container_identity_is_valid(container, workspace_id, path):
        _stop_unsafe_container(container, "workspace identity or storage binding mismatch")
        return {
            "workspace_id": workspace_id,
            "container_id": None,
            "storage_ref": names_for(workspace_id)[1],
            "state": "failed",
        }
    if container.status not in {"created", "running", "paused", "restarting", "exited", "dead"}:
        return {
            "workspace_id": workspace_id,
            "container_id": None,
            "storage_ref": names_for(workspace_id)[1],
            "state": "failed",
        }
    return _workspace_result(workspace_id, container)


def delete_workspace_container(workspace_id: str) -> dict:
    container_name, _ = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        path = workspace_path_for(workspace_id)
        if not _container_identity_is_valid(container, workspace_id, path):
            _stop_unsafe_container(container, "workspace identity or storage binding mismatch")
            raise HTTPException(status_code=409, detail="Workspace container is unsafe.")
        container.remove(force=True, v=True)
    except NotFound:
        pass
    return {**_workspace_result(workspace_id), "state": "deleted"}


def list_managed_workspace_ids() -> list[str]:
    containers = docker_client.containers.list(
        all=True, filters={"label": "devbox.managed=true"}
    )
    workspace_ids: list[str] = []
    for container in containers:
        try:
            container.reload()
            labels = container.attrs.get("Config", {}).get("Labels") or {}
            workspace_id = labels.get("devbox.workspace_id")
            if labels.get("devbox.managed") == "true" and workspace_id:
                names_for(workspace_id)
                workspace_ids.append(workspace_id)
            else:
                _stop_unsafe_container(container, "managed workspace labels are missing")
        except Exception as exc:
            _stop_unsafe_container(container, f"managed workspace inspection failed: {exc}")
    return workspace_ids


def reconcile_orphaned_git_helpers() -> int:
    helpers = docker_client.containers.list(
        all=True, filters={"label": "devbox.git_helper=true"}
    )
    removed = 0
    for helper in helpers:
        try:
            helper.reload()
            if helper.status in {"exited", "dead"}:
                helper.remove(force=True)
                removed += 1
        except NotFound:
            continue
        except DockerException:
            logger.exception(
                "Unable to clean up completed Git helper",
                extra={"container_id": getattr(helper, "id", None)},
            )
    return removed


def run_git_helper(workspace_id: str, request: GitHelperRequest) -> str:
    validate_git_arguments(request.arguments, request.network_enabled)
    path = workspace_path_for(workspace_id)
    environment = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C.UTF-8",
    }
    network_mode = "none"
    if request.network_enabled:
        network_mode = GIT_EGRESS_NETWORK
        environment.update(
            {
                "HTTP_PROXY": GIT_PROXY_URL,
                "HTTPS_PROXY": GIT_PROXY_URL,
                "http_proxy": GIT_PROXY_URL,
                "https_proxy": GIT_PROXY_URL,
            }
        )
    container = None
    try:
        container = docker_client.containers.create(
            image=IMAGE_NAME,
            command=[
                "timeout",
                "--signal=TERM",
                "--kill-after=5s",
                f"{GIT_OPERATION_TIMEOUT_SECONDS}s",
                "git",
                "--literal-pathspecs",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.pager=cat",
                "-c",
                "commit.gpgsign=false",
                *request.arguments,
            ],
            detach=True,
            user="10001:10001",
            working_dir="/workspace",
            environment=environment,
            network_mode=network_mode,
            volumes={str(path): {"bind": "/workspace", "mode": "rw"}},
            mem_limit="512m",
            nano_cpus=500_000_000,
            pids_limit=64,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            tmpfs={
                "/tmp": "rw,noexec,nosuid,size=64m",
                "/run": "rw,noexec,nosuid,size=8m",
            },
            init=True,
            labels={"devbox.git_helper": "true", "devbox.workspace_id": workspace_id},
        )
        container.start()
        try:
            result = container.wait(timeout=GIT_OPERATION_TIMEOUT_SECONDS)
        except Exception as exc:
            try:
                container.stop(timeout=0)
            except Exception:
                logger.exception("Unable to stop timed-out Git helper")
            raise HTTPException(
                status_code=504,
                detail="Git operation exceeded its time limit.",
            ) from exc
        output = container.logs(stdout=True, stderr=True, stream=True)
        collected = bytearray()
        if isinstance(output, bytes):
            collected.extend(output[-GIT_OUTPUT_LIMIT_BYTES:])
        else:
            for chunk in output:
                remaining = GIT_OUTPUT_LIMIT_BYTES - len(collected)
                if remaining <= 0:
                    break
                collected.extend(chunk[:remaining])
        decoded = collected.decode("utf-8", errors="replace")
        if result.get("StatusCode") != 0:
            raise HTTPException(status_code=422, detail=decoded.strip() or "Git operation failed.")
        return decoded.strip()
    except HTTPException:
        raise
    except (APIError, DockerException) as exc:
        logger.exception(
            "Git helper could not execute",
            extra={
                "workspace_id": workspace_id,
            },
        )
        raise HTTPException(
            status_code=503,
            detail="Git execution service is unavailable.",
        ) from exc
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                logger.exception(
                    "Unable to remove temporary Git helper",
                    extra={"workspace_id": workspace_id},
                )


def reconcile_managed_containers() -> int:
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
            expected_name, _ = names_for(workspace_id)
            if container.name != expected_name:
                _stop_unsafe_container(container, "container name does not match workspace ID")
                stopped += 1
                continue
            path = workspace_path_for(workspace_id)
            if not _container_identity_is_valid(container, workspace_id, path):
                _stop_unsafe_container(container, "workspace mount does not match assigned storage")
                stopped += 1
        except Exception as exc:
            _stop_unsafe_container(container, f"workspace validation failed: {exc}")
            stopped += 1
    return stopped


async def reconciliation_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(reconcile_managed_containers)
            await asyncio.to_thread(reconcile_orphaned_git_helpers)
        except Exception:
            logger.exception("Broker reconciliation loop failed")
        await asyncio.sleep(max(5, int(os.getenv("DEVBOX_RECONCILE_INTERVAL_SECONDS", "60"))))


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_service_token()
    ensure_image()
    reconciler = asyncio.create_task(reconciliation_loop())
    try:
        yield
    finally:
        reconciler.cancel()
        await asyncio.gather(reconciler, return_exceptions=True)


app = FastAPI(title="DevBox Privileged Broker", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        docker_client.ping()
    except DockerException as exc:
        raise HTTPException(status_code=503, detail="Docker daemon unavailable.") from exc
    return {"status": "ready"}


@app.post(
    "/internal/workspaces/{workspace_id}/start",
    dependencies=[Depends(require_service_token)],
)
def api_start_workspace(workspace_id: str) -> dict:
    return start_workspace(workspace_id)


@app.post(
    "/internal/workspaces/{workspace_id}/stop",
    dependencies=[Depends(require_service_token)],
)
def api_stop_workspace(workspace_id: str) -> dict:
    return stop_workspace(workspace_id)


@app.post(
    "/internal/workspaces/{workspace_id}/pause",
    dependencies=[Depends(require_service_token)],
)
def api_pause_workspace(workspace_id: str) -> dict:
    return pause_workspace(workspace_id)


@app.post(
    "/internal/workspaces/{workspace_id}/resume",
    dependencies=[Depends(require_service_token)],
)
def api_resume_workspace(workspace_id: str) -> dict:
    return resume_workspace(workspace_id)


@app.delete(
    "/internal/workspaces/{workspace_id}",
    dependencies=[Depends(require_service_token)],
)
def api_delete_workspace(workspace_id: str) -> dict:
    return delete_workspace_container(workspace_id)


@app.get(
    "/internal/workspaces/{workspace_id}/status",
    dependencies=[Depends(require_service_token)],
)
def api_workspace_status(workspace_id: str) -> dict:
    return inspect_workspace(workspace_id)


@app.get(
    "/internal/managed-workspaces",
    dependencies=[Depends(require_service_token)],
)
def api_managed_workspace_ids() -> dict:
    return {"workspace_ids": list_managed_workspace_ids()}


@app.post(
    "/internal/workspaces/{workspace_id}/git",
    dependencies=[Depends(require_service_token)],
)
def api_run_git_helper(workspace_id: str, payload: GitHelperRequest) -> dict:
    return {
        "workspace_id": workspace_id,
        "output": run_git_helper(workspace_id, payload),
    }


@app.post(
    "/internal/reconcile/git-helpers",
    dependencies=[Depends(require_service_token)],
)
def api_reconcile_git_helpers() -> dict:
    return {"removed": reconcile_orphaned_git_helpers()}
