import errno
import os
from pathlib import Path
from uuid import uuid4

import pytest

from worker.storage import XFSWorkspaceStorage

BROKER_STATE_ROOT = Path(os.getenv("DEVBOX_TEST_BROKER_STATE_ROOT", "/tmp/devbox-broker-state"))


def broker_registry(name: str) -> Path:
    BROKER_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    return BROKER_STATE_ROOT / name


XFS_TEST_ROOT = os.getenv("DEVBOX_TEST_XFS_ROOT")
pytestmark = pytest.mark.skipif(
    not XFS_TEST_ROOT,
    reason="Set DEVBOX_TEST_XFS_ROOT to an empty dedicated XFS project-quota mount.",
)


def new_workspace_id() -> str:
    return f"dbx_{uuid4().hex}"


def test_kernel_enforces_workspace_block_quota():
    storage = XFSWorkspaceStorage(
        XFS_TEST_ROOT,
        "16m",
        inode_limit=1000,
        registry_path=broker_registry("workspace-projects.sqlite3"),
    )
    workspace_id = new_workspace_id()
    directory = storage.ensure_workspace(workspace_id)
    exceeded = False

    try:
        with (directory / "quota-probe.bin").open("wb") as target:
            for _ in range(64):
                try:
                    target.write(b"x" * (1024 * 1024))
                    target.flush()
                    os.fsync(target.fileno())
                except OSError as exc:
                    assert exc.errno in {errno.EDQUOT, errno.ENOSPC}
                    exceeded = True
                    break
        assert exceeded, "XFS did not enforce the configured 16 MiB hard block quota."
    finally:
        storage.delete_workspace(workspace_id)


def test_kernel_enforces_workspace_inode_quota():
    storage = XFSWorkspaceStorage(
        XFS_TEST_ROOT,
        "64m",
        inode_limit=16,
        registry_path=broker_registry("workspace-projects.sqlite3"),
    )
    workspace_id = new_workspace_id()
    directory = storage.ensure_workspace(workspace_id)
    exceeded = False

    try:
        for index in range(128):
            try:
                (directory / f"empty-{index}").touch()
            except OSError as exc:
                assert exc.errno in {errno.EDQUOT, errno.ENOSPC}
                exceeded = True
                break
        assert exceeded, "XFS did not enforce the configured hard inode quota."
    finally:
        storage.delete_workspace(workspace_id)
