import errno
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from broker import main as broker
from worker.storage import XFSWorkspaceStorage

WORKSPACE_ROOT = os.getenv("DEVBOX_TEST_XFS_ROOT")
SNAPSHOT_ROOT = os.getenv("DEVBOX_TEST_SNAPSHOT_XFS_ROOT")
BROKER_STATE_ROOT = Path(
    os.getenv("DEVBOX_TEST_BROKER_STATE_ROOT", "/tmp/devbox-broker-state")
)

pytestmark = pytest.mark.skipif(
    not WORKSPACE_ROOT or not SNAPSHOT_ROOT,
    reason="Dedicated workspace and snapshot XFS mounts are required.",
)


def new_workspace_id() -> str:
    return f"dbx_{uuid4().hex}"


def make_backends():
    BROKER_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    workspace = XFSWorkspaceStorage(
        WORKSPACE_ROOT,
        "16m",
        inode_limit=1000,
        registry_path=BROKER_STATE_ROOT / "workspace-projects.sqlite3",
        shared_group_id=10001,
    )
    snapshot = XFSWorkspaceStorage(
        SNAPSHOT_ROOT,
        "32m",
        inode_limit=2000,
        registry_path=BROKER_STATE_ROOT / "snapshot-projects.sqlite3",
        shared_group_id=10001,
    )
    return workspace, snapshot


def test_broker_storage_rpc_enforces_xfs_quota_and_registry_isolation(monkeypatch):
    workspace_storage, snapshot_storage = make_backends()
    monkeypatch.setattr(
        broker,
        "storage_backends",
        {"workspace": workspace_storage, "snapshot": snapshot_storage},
    )
    workspace_id = new_workspace_id()
    snapshot_id = new_workspace_id()
    stage = None
    backup = None

    try:
        assert broker.api_validate_storage("workspace")["status"] == "ready"
        assert broker.api_validate_storage("snapshot")["status"] == "ready"

        workspace_path = Path(
            broker.api_ensure_storage_workspace("workspace", workspace_id)["path"]
        )
        broker.api_set_storage_quota(
            "workspace", workspace_id, broker.QuotaRequest(multiplier=1)
        )

        exceeded = False
        with (workspace_path / "quota-probe.bin").open("wb") as target:
            for _ in range(64):
                try:
                    target.write(b"x" * (1024 * 1024))
                    target.flush()
                    os.fsync(target.fileno())
                except OSError as exc:
                    assert exc.errno in {errno.EDQUOT, errno.ENOSPC}
                    exceeded = True
                    break
        assert exceeded, "Broker did not enforce the workspace XFS hard quota."

        prepared = broker.api_prepare_restore_directory(
            "workspace", workspace_id, broker.QuotaRequest(multiplier=2)
        )
        stage = Path(prepared["path"])
        assert stage.is_dir()
        backup = workspace_path.parent / f".{workspace_path.name}.restore-backup"
        swap_script = (
            "import os, shutil; "
            f"os.replace({str(workspace_path)!r}, {str(backup)!r}); "
            f"os.replace({str(stage)!r}, {str(workspace_path)!r}); "
            f"shutil.rmtree({str(backup)!r})"
        )
        subprocess.run(
            [
                "setpriv",
                "--reuid=10001",
                "--regid=10001",
                "--clear-groups",
                "python3",
                "-c",
                swap_script,
            ],
            check=True,
        )
        broker.api_mark_storage_initialized("workspace", workspace_id)

        snapshot_path = Path(
            broker.api_ensure_storage_workspace("snapshot", snapshot_id)["path"]
        )
        assert snapshot_path.is_dir()

        assert workspace_storage.registry.is_file()
        assert snapshot_storage.registry.is_file()
        assert not (Path(WORKSPACE_ROOT) / ".devbox-storage.sqlite3").exists()
        assert not (Path(SNAPSHOT_ROOT) / ".devbox-storage.sqlite3").exists()
    finally:
        if stage and stage.exists():
            shutil.rmtree(stage)
        if backup and backup.exists():
            shutil.rmtree(backup)
        broker.api_delete_storage_workspace("workspace", workspace_id)
        broker.api_delete_storage_workspace("snapshot", snapshot_id)
