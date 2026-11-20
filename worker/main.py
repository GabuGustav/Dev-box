import hashlib
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from fastapi import Depends, FastAPI, Header, HTTPException, status

logger = logging.getLogger("devbox.executor")
WORKSPACE_ID_PATTERN = re.compile(r"^dbx_[A-Za-z0-9_-]{20,40}$")
IMAGE_NAME = os.getenv("DEVBOX_WORKSPACE_IMAGE", "devbox-workspace:0.1")
WORKSPACE_CPU = float(os.getenv("DEVBOX_WORKSPACE_CPU", "1.0"))
WORKSPACE_MEMORY = os.getenv("DEVBOX_WORKSPACE_MEMORY", "1g")
WORKSPACE_PIDS = int(os.getenv("DEVBOX_WORKSPACE_PIDS", "256"))
SERVICE_TOKEN = os.getenv("DEVBOX_EXECUTOR_TOKEN", "")
WORKSPACE_IMAGE_CONTEXT = os.getenv("DEVBOX_WORKSPACE_IMAGE_CONTEXT", "/opt/devbox/workspace-image")
docker_client = docker.from_env()


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {SERVICE_TOKEN}"
    if not SERVICE_TOKEN or not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def names_for(workspace_id: str) -> tuple[str, str]:
    if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise HTTPException(status_code=422, detail="Invalid workspace ID.")
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
    return f"devbox-ws-{digest}", f"devbox-vol-{digest}"


def ensure_image() -> None:
    try:
        docker_client.images.get(IMAGE_NAME)
    except ImageNotFound:
        docker_client.images.build(path=WORKSPACE_IMAGE_CONTEXT, tag=IMAGE_NAME, rm=True)


def inspect_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        state = container.status
        return {
            "workspace_id": workspace_id,
            "container_id": container.id,
            "volume_name": volume_name,
            "state": state,
        }
    except NotFound:
        return {
            "workspace_id": workspace_id,
            "container_id": None,
            "volume_name": volume_name,
            "state": "absent",
        }


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not SERVICE_TOKEN:
        logger.warning("DEVBOX_EXECUTOR_TOKEN is not configured; internal calls will be rejected.")
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


@app.post("/internal/workspaces/{workspace_id}/start", dependencies=[Depends(require_service_token)])
def start_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        volume = docker_client.volumes.get(volume_name)
    except NotFound:
        volume = docker_client.volumes.create(name=volume_name, labels={"devbox.managed": "true"})
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
    except NotFound:
        container = docker_client.containers.create(
            image=IMAGE_NAME,
            name=container_name,
            command=["sleep", "infinity"],
            detach=True,
            user="10001:10001",
            working_dir="/workspace",
            network_mode="none",
            volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
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
        "volume_name": volume.name,
        "state": container.status,
    }


@app.post("/internal/workspaces/{workspace_id}/stop", dependencies=[Depends(require_service_token)])
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
        return {"workspace_id": workspace_id, "container_id": container.id,
                "volume_name": volume_name, "state": container.status}
    except NotFound:
        return {"workspace_id": workspace_id, "container_id": None,
                "volume_name": volume_name, "state": "absent"}


@app.post("/internal/workspaces/{workspace_id}/pause", dependencies=[Depends(require_service_token)])
def pause_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.reload()
        if container.status == "running":
            container.pause()
        container.reload()
        return {"workspace_id": workspace_id, "container_id": container.id,
                "volume_name": volume_name, "state": container.status}
    except NotFound:
        raise HTTPException(status_code=409, detail="Workspace executor does not exist.")


@app.post("/internal/workspaces/{workspace_id}/resume", dependencies=[Depends(require_service_token)])
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
        return {"workspace_id": workspace_id, "container_id": container.id,
                "volume_name": volume_name, "state": container.status}
    except NotFound:
        raise HTTPException(status_code=409, detail="Workspace executor does not exist.")


@app.delete("/internal/workspaces/{workspace_id}", dependencies=[Depends(require_service_token)])
def delete_workspace(workspace_id: str) -> dict:
    container_name, volume_name = names_for(workspace_id)
    try:
        container = docker_client.containers.get(container_name)
        container.remove(force=True, v=True)
    except NotFound:
        pass
    try:
        volume = docker_client.volumes.get(volume_name)
        volume.remove(force=True)
    except NotFound:
        pass
    return {"workspace_id": workspace_id, "container_id": None,
            "volume_name": volume_name, "state": "deleted"}
