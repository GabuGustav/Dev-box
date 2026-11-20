import os
from uuid import uuid4

import docker
import pytest

from worker.storage import XFSWorkspaceStorage


XFS_TEST_ROOT = os.getenv("DEVBOX_TEST_XFS_ROOT")
DOCKER_INTEGRATION = os.getenv("DEVBOX_RUN_DOCKER_INTEGRATION") == "1"
IMAGE = os.getenv("DEVBOX_TEST_WORKSPACE_IMAGE", "python:3.12-slim")

pytestmark = pytest.mark.skipif(
    not XFS_TEST_ROOT or not DOCKER_INTEGRATION,
    reason="Set DEVBOX_TEST_XFS_ROOT and DEVBOX_RUN_DOCKER_INTEGRATION=1 on a Linux Docker host.",
)


def new_workspace_id() -> str:
    return f"dbx_{uuid4().hex}"


def run_workspace(client, image, directory, command):
    return client.containers.run(
        image,
        command=["python", "-c", command],
        volumes={str(directory): {"bind": "/workspace", "mode": "rw"}},
        working_dir="/workspace",
        user="10001:10001",
        network_mode="none",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        detach=False,
        remove=True,
    )


def test_workspace_mount_is_private_and_survives_container_replacement():
    client = docker.from_env()
    try:
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"Docker daemon unavailable: {exc}")

    storage = XFSWorkspaceStorage(XFS_TEST_ROOT, "128m", inode_limit=5000)
    first_id = new_workspace_id()
    second_id = new_workspace_id()
    created = []
    try:
        first_path = storage.ensure_workspace(first_id)
        created.append(first_id)
        second_path = storage.ensure_workspace(second_id)
        created.append(second_id)
        (second_path / "private.txt").write_text("agent-b-secret", encoding="utf-8")

        output = run_workspace(
            client,
            IMAGE,
            first_path,
            "from pathlib import Path; "
            "p=Path('/workspace/private.txt'); "
            "raise SystemExit(1 if p.exists() else 0)",
        )
        assert output == b""

        run_workspace(
            client,
            IMAGE,
            first_path,
            "from pathlib import Path; "
            "Path('/workspace/persist.txt').write_text('persisted-state')",
        )
        output = run_workspace(
            client,
            IMAGE,
            first_path,
            "from pathlib import Path; "
            "assert Path('/workspace/persist.txt').read_text() == 'persisted-state'",
        )
        assert output == b""
    finally:
        try:
            for workspace_id in reversed(created):
                storage.delete_workspace(workspace_id)
        finally:
            client.close()
