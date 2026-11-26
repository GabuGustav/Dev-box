from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from worker.storage import WorkspaceStorageError, XFSWorkspaceStorage

logger = logging.getLogger("devbox.broker")

WORKSPACE_ID_PATTERN = re.compile(r"^dbx_[A-Za-z0-9_-]{20,40}$")
SERVICE_TOKEN = os.getenv("DEVBOX_BROKER_TOKEN", "")
IMAGE_NAME = os.getenv("DEVBOX_WORKSPACE_IMAGE", "devbox-workspace:0.2")
IMAGE_CONTEXT = os.getenv("DEVBOX_WORKSPACE_IMAGE_CONTEXT", "/opt/devbox/workspace-image")
WORKSPACE_STORAGE_ROOT = Path(
    os.getenv("DEVBOX_WORKSPACE_STORAGE_ROOT", "/srv/devbox/workspaces")
)
SNAPSHOT_STORAGE_ROOT = Path(
    os.getenv("DEVBOX_SNAPSHOT_STORAGE_ROOT", "/srv/devbox/snapshots")
)
BROKER_STATE_ROOT = Path(
    os.getenv("DEVBOX_BROKER_STATE_ROOT", "/var/lib/devbox-broker")
)
WORKER_STATE_ROOT = Path(
    os.getenv("DEVBOX_WORKER_STATE_ROOT", "/var/lib/devbox-worker")
)
WORKSPACE_QUOTA = os.getenv("DEVBOX_WORKSPACE_DISK_QUOTA", "10g")
SNAPSHOT_QUOTA = os.getenv("DEVBOX_SNAPSHOT_DISK_QUOTA", "20g")
WORKSPACE_INODE_LIMIT = int(os.getenv("DEVBOX_WORKSPACE_INODE_LIMIT", "200000"))
SNAPSHOT_INODE_LIMIT = int(os.getenv("DEVBOX_SNAPSHOT_INODE_LIMIT", "300000"))
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

storage_backends = {
    "workspace": XFSWorkspaceStorage(
        str(WORKSPACE_STORAGE_ROOT),
        WORKSPACE_QUOTA,
        WORKSPACE_INODE_LIMIT,
        registry_path=BROKER_STATE_ROOT / "workspace-projects.sqlite3",
        shared_group_id=10001,
        directory_mode=0o2770,
    ),
    "snapshot": XFSWorkspaceStorage(
        str(SNAPSHOT_STORAGE_ROOT),
        SNAPSHOT_QUOTA,
        SNAPSHOT_INODE_LIMIT,
        registry_path=BROKER_STATE_ROOT / "snapshot-projects.sqlite3",
        shared_group_id=10001,
        directory_mode=0o2770,
    ),
}


class GitHelperRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: list[str] = Field(min_length=1, max_length=110)
    network_enabled: bool = False


class QuotaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    multiplier: int = Field(ge=1, le=10)


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
    container_name, storage_ref = names_for(workspace_id)
    labels = {}
    mounts = []
    if container is not None:
        labels = container.attrs.get("Config", {}).get("Labels") or {}
        mounts = container.attrs.get("Mounts", [])
    return {
        "workspace_id": workspace_id,
        "container_id": container.id if container else None,
        "container_name": container.name if container else container_name,
        "storage_ref": storage_ref,
        "state": container.status if container else "absent",
        "attrs": {"Config": {"Labels": labels}, "Mounts": mounts},
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
            privileged=False,
            devices=[],
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
    is_clone = request.arguments[0] == "clone"
    if not is_clone:
        git_directory = path / ".git"
        if git_directory.is_symlink() or not git_directory.is_dir():
            raise HTTPException(status_code=409, detail="Workspace Git metadata is missing or unsafe.")
    environment = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_PAGER": "cat",
        "GIT_ALLOW_PROTOCOL": "https",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C.UTF-8",
    }
    if not is_clone:
        environment.update({"GIT_DIR": "/workspace/.git", "GIT_WORK_TREE": "/workspace"})
    git_config = [
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.pager=cat",
        "-c", "core.sshCommand=/bin/false",
        "-c", "credential.helper=",
        "-c", "credential.interactive=false",
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ext.allow=never",
        "-c", "protocol.ssh.allow=never",
        "-c", "protocol.git.allow=never",
        "-c", "submodule.recurse=false",
        "-c", "fetch.recurseSubmodules=false",
        "-c", "commit.gpgsign=false",
    ]
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
        git_config.extend(["-c", f"http.proxy={GIT_PROXY_URL}"])
    else:
        git_config.extend(["-c", "http.proxy="])
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
                "--no-pager",
                "--literal-pathspecs",
                *git_config,
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


def prepare_worker_state() -> None:
    if WORKER_STATE_ROOT.is_symlink():
        raise RuntimeError("Worker state root must not be a symbolic link.")
    WORKER_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    if not WORKER_STATE_ROOT.is_dir():
        raise RuntimeError("Worker state root must be a directory.")
    os.chown(WORKER_STATE_ROOT, 10002, 10001)
    WORKER_STATE_ROOT.chmod(0o700)
    allowed_files = {
        "snapshots.sqlite3",
        "snapshots.sqlite3-journal",
        "snapshots.sqlite3-wal",
        "snapshots.sqlite3-shm",
    }
    for entry in WORKER_STATE_ROOT.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise RuntimeError("Worker state contains an unexpected entry.")
        if entry.name.startswith("snapshots.sqlite3.migrating"):
            entry.unlink()
            continue
        if entry.name not in allowed_files:
            raise RuntimeError("Worker state contains an unexpected entry.")
        os.chown(entry, 10002, 10001)
        entry.chmod(0o600)


def prepare_shared_storage_permissions(storage: XFSWorkspaceStorage) -> None:
    root = storage.root
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Configured XFS storage root is missing or unsafe.")
    # validate_backend has already assigned root:10001 ownership and mode 2750.
    if root.stat().st_uid != 0 or root.stat().st_gid != 10001:
        raise RuntimeError("XFS storage root does not have broker-owned group permissions.")
    root.chmod(0o2750)
    legacy_metadata = root / ".devbox-snapshots.sqlite3"
    if legacy_metadata.exists() or legacy_metadata.is_symlink():
        if legacy_metadata.is_symlink() or not legacy_metadata.is_file():
            raise RuntimeError("Legacy snapshot metadata registry is unsafe.")
        journal = legacy_metadata.with_name(legacy_metadata.name + "-journal")
        if journal.exists() or journal.is_symlink():
            raise RuntimeError("Legacy snapshot metadata has an unfinished SQLite journal.")
        os.chown(legacy_metadata, 0, 10001)
        legacy_metadata.chmod(0o640)
        for suffix in ("-wal", "-shm"):
            sidecar = legacy_metadata.with_name(legacy_metadata.name + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise RuntimeError("Legacy snapshot metadata sidecar is unsafe.")
                os.chown(sidecar, 0, 10001)
                sidecar.chmod(0o640)


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_service_token()
    for storage in storage_backends.values():
        storage.validate_backend()
        prepare_shared_storage_permissions(storage)
    prepare_worker_state()
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


@app.get(
    "/internal/ready",
    dependencies=[Depends(require_service_token)],
)
def internal_ready() -> dict[str, str]:
    try:
        docker_client.ping()
    except DockerException as exc:
        raise HTTPException(
            status_code=503,
            detail="Broker Docker service is unavailable.",
        ) from exc
    return {"status": "ready"}


def _storage_for(storage_kind: Literal["workspace", "snapshot"]) -> XFSWorkspaceStorage:
    return storage_backends[storage_kind]


def _storage_operation(operation):
    try:
        return operation()
    except WorkspaceStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get(
    "/internal/storage/{storage_kind}/validate",
    dependencies=[Depends(require_service_token)],
)
def api_validate_storage(
    storage_kind: Literal["workspace", "snapshot"],
) -> dict[str, str]:
    _storage_operation(lambda: _storage_for(storage_kind).validate_backend())
    return {"storage_kind": storage_kind, "status": "ready"}


@app.post(
    "/internal/storage/{storage_kind}/{workspace_id}/ensure",
    dependencies=[Depends(require_service_token)],
)
def api_ensure_storage_workspace(
    storage_kind: Literal["workspace", "snapshot"],
    workspace_id: str,
) -> dict[str, str]:
    directory = _storage_operation(
        lambda: _storage_for(storage_kind).ensure_workspace(workspace_id)
    )
    return {"workspace_id": workspace_id, "path": str(directory)}


@app.put(
    "/internal/storage/{storage_kind}/{workspace_id}/quota",
    dependencies=[Depends(require_service_token)],
)
def api_set_storage_quota(
    storage_kind: Literal["workspace", "snapshot"],
    workspace_id: str,
    payload: QuotaRequest,
) -> dict[str, str | int]:
    storage = _storage_for(storage_kind)
    _storage_operation(storage.validate_backend)
    _storage_operation(
        lambda: storage.set_project_quota(workspace_id, multiplier=payload.multiplier)
    )
    return {
        "workspace_id": workspace_id,
        "storage_kind": storage_kind,
        "multiplier": payload.multiplier,
    }


@app.post(
    "/internal/storage/{storage_kind}/{workspace_id}/prepare-restore",
    dependencies=[Depends(require_service_token)],
)
def api_prepare_restore_directory(
    storage_kind: Literal["workspace", "snapshot"],
    workspace_id: str,
    payload: QuotaRequest,
) -> dict[str, str]:
    storage = _storage_for(storage_kind)
    _storage_operation(storage.validate_backend)
    _, workspace_directory = storage._workspace_path(workspace_id)
    staging_directory = storage.root / f".{workspace_directory.name}.restore-stage"
    _storage_operation(
        lambda: storage.prepare_project_directory(
            workspace_id,
            staging_directory,
            storage._project_id(workspace_id),
            quota_multiplier=payload.multiplier,
        )
    )
    return {"workspace_id": workspace_id, "path": str(staging_directory)}


@app.post(
    "/internal/storage/{storage_kind}/{workspace_id}/mark-initialized",
    dependencies=[Depends(require_service_token)],
)
def api_mark_storage_initialized(
    storage_kind: Literal["workspace", "snapshot"],
    workspace_id: str,
) -> dict[str, str]:
    storage = _storage_for(storage_kind)
    _storage_operation(storage.validate_backend)
    _, directory = storage._workspace_path(workspace_id)
    _storage_operation(
        lambda: storage.mark_workspace_initialized(workspace_id, directory)
    )
    return {"workspace_id": workspace_id, "status": "initialized"}


def _fsync_storage_root(root: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    descriptor = os.open(root, os.O_RDONLY | directory_flag)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@app.post(
    "/internal/storage/{storage_kind}/{workspace_id}/recover-restore",
    dependencies=[Depends(require_service_token)],
)
def api_recover_storage_restore(
    storage_kind: Literal["workspace", "snapshot"],
    workspace_id: str,
) -> dict[str, str]:
    storage = _storage_for(storage_kind)
    _storage_operation(storage.validate_backend)
    _, workspace = storage._workspace_path(workspace_id)
    stage = storage.root / f".{workspace.name}.restore-stage"
    backup = storage.root / f".{workspace.name}.restore-backup"
    for path in (workspace, stage, backup):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise HTTPException(status_code=409, detail="Restore path is unsafe.")
        if path.parent.resolve() != storage.root.resolve():
            raise HTTPException(status_code=409, detail="Restore path escaped its storage root.")
    restore_state_exists = backup.exists() or stage.exists()
    try:
        if backup.exists():
            if workspace.exists():
                storage.mark_workspace_initialized(workspace_id, workspace)
                shutil.rmtree(backup)
            else:
                os.replace(backup, workspace)
                _fsync_storage_root(storage.root)
                storage.mark_workspace_initialized(workspace_id, workspace)
        elif workspace.exists():
            storage.mark_workspace_initialized(workspace_id, workspace)
        if stage.exists():
            shutil.rmtree(stage)
        if workspace.exists() or restore_state_exists:
            storage.set_project_quota(workspace_id, multiplier=1)
        if restore_state_exists:
            _fsync_storage_root(storage.root)
    except (OSError, WorkspaceStorageError) as exc:
        raise HTTPException(status_code=503, detail="Unable to recover workspace restore.") from exc
    return {"workspace_id": workspace_id, "state": "recovered"}


@app.post(
    "/internal/storage/{storage_kind}/{workspace_id}/commit-restore",
    dependencies=[Depends(require_service_token)],
)
def api_commit_storage_restore(
    storage_kind: Literal["workspace", "snapshot"],
    workspace_id: str,
) -> dict[str, str]:
    storage = _storage_for(storage_kind)
    _storage_operation(storage.validate_backend)
    _, workspace = storage._workspace_path(workspace_id)
    stage = storage.root / f".{workspace.name}.restore-stage"
    backup = storage.root / f".{workspace.name}.restore-backup"
    for path in (workspace, stage, backup):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise HTTPException(status_code=409, detail="Restore path is unsafe.")
        if path.parent.resolve() != storage.root.resolve():
            raise HTTPException(status_code=409, detail="Restore path escaped its storage root.")
    if not stage.is_dir() or backup.exists():
        raise HTTPException(status_code=409, detail="Restore staging state is invalid.")
    try:
        if workspace.exists():
            os.replace(workspace, backup)
            _fsync_storage_root(storage.root)
        try:
            os.replace(stage, workspace)
            _fsync_storage_root(storage.root)
            storage.mark_workspace_initialized(workspace_id, workspace)
            storage.set_project_quota(workspace_id, multiplier=1)
        except Exception:
            if workspace.exists() and backup.exists():
                shutil.rmtree(workspace)
            if backup.exists():
                os.replace(backup, workspace)
                _fsync_storage_root(storage.root)
                storage.mark_workspace_initialized(workspace_id, workspace)
                storage.set_project_quota(workspace_id, multiplier=1)
            raise
        if backup.exists():
            shutil.rmtree(backup)
            _fsync_storage_root(storage.root)
    except (OSError, WorkspaceStorageError) as exc:
        raise HTTPException(status_code=503, detail="Unable to commit workspace restore.") from exc
    return {"workspace_id": workspace_id, "state": "restored"}


@app.delete(
    "/internal/storage/{storage_kind}/{workspace_id}",
    dependencies=[Depends(require_service_token)],
)
def api_delete_storage_workspace(
    storage_kind: Literal["workspace", "snapshot"],
    workspace_id: str,
) -> dict[str, str]:
    _storage_operation(lambda: _storage_for(storage_kind).delete_workspace(workspace_id))
    return {"workspace_id": workspace_id, "status": "deleted"}
