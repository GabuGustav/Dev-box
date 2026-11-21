import os
import secrets

import pytest

from worker.snapshots import SnapshotStore
from worker.storage import XFSWorkspaceStorage

WORKSPACE_ROOT = os.getenv("DEVBOX_TEST_XFS_ROOT")
SNAPSHOT_ROOT = os.getenv("DEVBOX_TEST_SNAPSHOT_XFS_ROOT")
ENCRYPTION_KEY = os.getenv("DEVBOX_TEST_SNAPSHOT_ENCRYPTION_KEY")

pytestmark = pytest.mark.skipif(
    not WORKSPACE_ROOT or not SNAPSHOT_ROOT or not ENCRYPTION_KEY,
    reason=(
        "Set both dedicated XFS test mounts and a test snapshot encryption key "
        "to run the real-filesystem snapshot integration test."
    ),
)


def test_encrypted_snapshot_restore_on_separate_xfs_mounts():
    workspace_storage = XFSWorkspaceStorage(WORKSPACE_ROOT, "128m", inode_limit=5000)
    snapshot_storage = XFSWorkspaceStorage(SNAPSHOT_ROOT, "256m", inode_limit=5000)
    store = SnapshotStore(
        snapshot_storage,
        workspace_storage,
        ENCRYPTION_KEY,
        max_snapshots=3,
        max_snapshot_bytes=32 * 1024 * 1024,
    )
    workspace_id = f"dbx_{secrets.token_urlsafe(18)}"
    workspace = None

    try:
        workspace = workspace_storage.ensure_workspace(workspace_id)
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("print('snapshot')\n", encoding="utf-8")
        snapshot = store.create(workspace_id, workspace)

        (workspace / "src" / "main.py").write_text("print('modified')\n", encoding="utf-8")
        result = store.restore(workspace_id, snapshot["snapshot_id"], workspace)

        assert result["state"] == "restored"
        assert (workspace / "src" / "main.py").read_text(encoding="utf-8") == (
            "print('snapshot')\n"
        )
        assert store.list(workspace_id)[0]["snapshot_id"] == snapshot["snapshot_id"]
    finally:
        store.delete_workspace(workspace_id)
        if workspace is not None:
            workspace_storage.delete_workspace(workspace_id)
