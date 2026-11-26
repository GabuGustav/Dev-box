import os
import subprocess
from pathlib import Path
from uuid import uuid4

import docker
import pytest
from fastapi import HTTPException

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
        shared_group_id=10001,
        directory_mode=0o770,
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
        host_config = container.attrs["HostConfig"]
        assert host_config["ReadonlyRootfs"] is True
        assert host_config["CapDrop"] == ["ALL"]
        assert host_config["NetworkMode"] == "none"
        assert host_config["IpcMode"] == "private"
        assert host_config["PidsLimit"] == broker.WORKSPACE_PIDS
        assert host_config["Privileged"] is False
        assert host_config.get("Devices") in (None, [])
        assert host_config.get("PidMode") not in {"host"}
        security_opt = host_config.get("SecurityOpt") or []
        assert "no-new-privileges:true" in security_opt
        assert not any("unconfined" in option.lower() for option in security_opt)
        docker_security = client.info().get("SecurityOptions", [])
        assert any("seccomp" in option.lower() for option in docker_security)
        if any("apparmor" in option.lower() for option in docker_security):
            assert container.attrs.get("AppArmorProfile") == "docker-default"
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

        fsmonitor_script = workspace_path / "fsmonitor.sh"
        fsmonitor_marker = workspace_path / "fsmonitor-executed"
        fsmonitor_script.write_text(
            "#!/bin/sh\n"
            "printf 'executed' > /workspace/fsmonitor-executed\n"
            "cat >/dev/null\n"
            "printf 'token\\0'\n",
            encoding="utf-8",
        )
        fsmonitor_script.chmod(0o755)
        os.chown(fsmonitor_script, 10001, 10001)
        subprocess.run(
            ["git", "config", "--local", "core.fsmonitor", "/workspace/fsmonitor.sh"],
            cwd=workspace_path,
            check=True,
        )

        output = broker.run_git_helper(
            workspace_id,
            broker.GitHelperRequest(
                arguments=["status", "--short", "--branch"],
                network_enabled=False,
            ),
        )
        assert "No commits yet" in output or "On branch" in output
        assert not fsmonitor_marker.exists(), "Repository-local fsmonitor command was executed"

        bare_remote = workspace_path / "attacker.git"
        subprocess.run(["git", "init", "--bare", "--quiet", str(bare_remote)], check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
            cwd=workspace_path,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "config",
                "--local",
                "url.file:///workspace/attacker.git.insteadOf",
                "https://github.com/example/repo.git",
            ],
            cwd=workspace_path,
            check=True,
        )
        git_network = client.networks.create(
            name=f"devbox-git-helper-test-{uuid4().hex[:8]}",
            driver="bridge",
            internal=True,
        )
        monkeypatch.setattr(broker, "GIT_EGRESS_NETWORK", git_network.name)
        try:
            with pytest.raises(HTTPException) as fetch_error:
                broker.run_git_helper(
                    workspace_id,
                    broker.GitHelperRequest(
                        arguments=["fetch", "--prune", "origin"],
                        network_enabled=True,
                    ),
                )
            assert fetch_error.value.status_code == 422
            assert "transport 'file' not allowed" in str(fetch_error.value.detail)
        finally:
            git_network.remove()
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
