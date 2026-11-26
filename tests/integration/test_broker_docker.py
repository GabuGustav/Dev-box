import os
import subprocess
from pathlib import Path
from uuid import uuid4

import docker
import pytest

from broker import main as broker
from worker.storage import XFSWorkspaceStorage

WORKSPACE_ROOT = os.getenv("DEVBOX_TEST_XFS_ROOT")
BROKER_STATE_ROOT = Path(
    os.getenv("DEVBOX_TEST_BROKER_STATE_ROOT", "/tmp/devbox-broker-state")
)
DOCKER_INTEGRATION = os.getenv("DEVBOX_RUN_DOCKER_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not WORKSPACE_ROOT or not DOCKER_INTEGRATION,
    reason="A Linux Docker host and dedicated XFS workspace mount are required.",
)


def test_broker_owns_workspace_and_git_helper_container_lifecycle(monkeypatch):
    workspace_id = f"dbx_{uuid4().hex}"
    workspace_root = Path(WORKSPACE_ROOT)
    state_root = BROKER_STATE_ROOT
    state_root.mkdir(parents=True, exist_ok=True)
    storage = XFSWorkspaceStorage(
        str(workspace_root),
        "128m",
        inode_limit=5000,
        registry_path=state_root / "workspace-projects.sqlite3",
    )
    monkeypatch.setattr(broker, "WORKSPACE_STORAGE_ROOT", workspace_root)
    image_context = Path(__file__).resolve().parents[2] / "workspace-image"
    monkeypatch.setattr(broker, "IMAGE_CONTEXT", str(image_context))
    monkeypatch.setattr(broker, "IMAGE_NAME", "devbox-workspace:broker-integration")
    monkeypatch.setattr(
        broker,
        "storage_backends",
        {"workspace": storage, "snapshot": storage},
    )
    client = broker.docker_client
    container = None

    try:
        client.ping()
        workspace_path = storage.ensure_workspace(workspace_id)
        broker.ensure_image()
        started = broker.start_workspace(workspace_id)
        container = client.containers.get(started["container_name"])
        container.reload()

        assert started["state"] == "running"
        assert container.attrs["Config"]["User"] == "10001:10001"
        assert container.attrs["HostConfig"]["ReadonlyRootfs"] is True
        assert container.attrs["HostConfig"]["CapDrop"] == ["ALL"]
        assert container.attrs["HostConfig"]["NetworkMode"] == "none"
        assert container.attrs["HostConfig"]["IpcMode"] == "private"
        assert container.attrs["HostConfig"]["PidsLimit"] == broker.WORKSPACE_PIDS
        workspace_mount = next(
            mount
            for mount in container.attrs["Mounts"]
            if mount.get("Destination") == "/workspace"
        )
        assert workspace_mount["Source"] == str(workspace_path)

        subprocess.run(["git", "init", "--quiet"], cwd=workspace_path, check=True)
        for root, directories, files in os.walk(workspace_path):
            os.chown(root, 10001, 10001)
            for name in directories + files:
                os.chown(Path(root) / name, 10001, 10001)

        output = broker.run_git_helper(
            workspace_id,
            broker.GitHelperRequest(
                arguments=["status", "--short", "--branch"],
                network_enabled=False,
            ),
        )
        assert "No commits yet" in output or "On branch" in output
        assert client.containers.list(
            all=True,
            filters={
                "label": [
                    "devbox.git_helper=true",
                    f"devbox.workspace_id={workspace_id}",
                ]
            },
        ) == []

        stopped = broker.stop_workspace(workspace_id)
        assert stopped["state"] in {"exited", "created"}
        broker.delete_workspace_container(workspace_id)
        container = None
        assert client.containers.list(
            all=True,
            filters={"label": f"devbox.workspace_id={workspace_id}"},
        ) == []
    finally:
        if container is not None:
            try:
                container.remove(force=True, v=True)
            except docker.errors.DockerException:
                pass
        try:
            storage.delete_workspace(workspace_id)
        except Exception:
            pass
        try:
            client.images.remove("devbox-workspace:broker-integration", force=True)
        except docker.errors.DockerException:
            pass
