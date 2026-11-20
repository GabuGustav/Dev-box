import hashlib
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, status

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
workspace_storage = XFSWorkspaceStorage(
    WORKSPACE_STORAGE_ROOT, WORKSPACE_DISK_QUOTA, WORKSPACE_INODE_LIMIT
)
docker_client = docker.from_env()


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


def ensure_image() -> None:
    try:
        docker_client.images.get(IMAGE_NAME)
    except ImageNotFound:
        docker_client.images.build(path=WORKSPACE_IMAGE_CONTEXT, tag=IMAGE_NAME, rm=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not SERVICE_TOKEN:
        logger.warning(
            "DEVBOX_EXECUTOR_TOKEN is not configured; internal calls will be rejected."
        )
    ensure_image()
    yield


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
def start_workspace(workspace_id: str) -> dict:
    container_name, storage_name = names_for(workspace_id)
    try:
        workspace_path = workspace_storage.ensure_workspace(workspace_id)
    except WorkspaceStorageError as exc:
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
            "volume_name": volume_name,
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
def resume_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        if container.status == "paused":
            container.unpause()
        elif container.status != "running":
            container.start()
        container.reload()
        return {
            "workspace_id": workspace_id,
            "container_id": container.id,
            "volume_name": volume_name,
            "state": container.status,
        }
    except NotFound as exc:
        raise HTTPException(
            status_code=409,
            detail="Workspace executor does not exist.",
        ) from exc


@app.delete(
    "/internal/workspaces/{workspace_id}",
    dependencies=[Depends(require_service_token)],
)
def delete_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.remove(force=True, v=True)
    except NotFound:
        pass
    try:
        workspace_storage.delete_workspace(workspace_id)
    except WorkspaceStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "workspace_id": workspace_id,
        "container_id": None,
        "volume_name": volume_name,
        "state": "deleted",
    }
