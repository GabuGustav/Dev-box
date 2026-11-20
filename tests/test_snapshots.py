import hashlib
import secrets
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from worker.snapshots import SnapshotError, SnapshotStore
from worker.storage import XFSWorkspaceStorage


WORKSPACE_ID = "dbx_abcdefghijklmnopqrstuv"


def make_storage(root: Path) -> XFSWorkspaceStorage:
    root.mkdir(parents=True, exist_ok=True)
    storage = XFSWorkspaceStorage(
        str(root),
        "10g",
        runner=lambda *args, **kwargs: SimpleNamespace(stdout=""),
        owner_setter=lambda *_: None,
    )
    storage.validate_backend = lambda: None
    return storage


def make_store(tmp_path: Path, *, max_snapshots: int = 10):
    workspace_storage = make_storage(tmp_path / "workspace-data")
    snapshot_storage = make_storage(tmp_path / "snapshot-data")
    store = SnapshotStore(
        snapshot_storage,
        workspace_storage,
        secrets.token_urlsafe(32),
        max_snapshots=max_snapshots,
        max_snapshot_bytes=16 * 1024 * 1024,
    )
    return store, workspace_storage


def test_snapshot_round_trip_restores_exact_workspace_contents(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("print('before')\n", encoding="utf-8")
    (workspace / "remove-me.txt").write_text("old", encoding="utf-8")

    snapshot = store.create(WORKSPACE_ID, workspace)

    (workspace / "src" / "main.py").write_text("print('after')\n", encoding="utf-8")
    (workspace / "remove-me.txt").unlink()
    (workspace / "new-file.txt").write_text("new", encoding="utf-8")

    result = store.restore(WORKSPACE_ID, snapshot["snapshot_id"], workspace)

    assert result["state"] == "restored"
    assert (workspace / "src" / "main.py").read_text(encoding="utf-8") == "print('before')\n"
    assert (workspace / "remove-me.txt").read_text(encoding="utf-8") == "old"
    assert not (workspace / "new-file.txt").exists()
    assert store.list(WORKSPACE_ID)[0]["snapshot_id"] == snapshot["snapshot_id"]


def test_snapshot_is_encrypted_and_authenticated(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "secret.txt").write_text("plaintext-secret-marker", encoding="utf-8")

    snapshot = store.create(WORKSPACE_ID, workspace)
    path, _ = store._snapshot_record(WORKSPACE_ID, snapshot["snapshot_id"])
    encrypted = path.read_bytes()

    assert b"plaintext-secret-marker" not in encrypted
    assert encrypted.startswith(b"DBXSNP1\0")


def test_tampered_authenticated_snapshot_does_not_replace_workspace(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "important.txt").write_text("keep-this", encoding="utf-8")
    snapshot = store.create(WORKSPACE_ID, workspace)
    path, _ = store._snapshot_record(WORKSPACE_ID, snapshot["snapshot_id"])

    content = bytearray(path.read_bytes())
    content[-20] ^= 0x01
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    connection = sqlite3.connect(store.registry)
    try:
        connection.execute(
            "UPDATE snapshots SET sha256 = ? WHERE snapshot_id = ?",
            (digest, snapshot["snapshot_id"]),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SnapshotError, match="authentication"):
        store.restore(WORKSPACE_ID, snapshot["snapshot_id"], workspace)

    assert (workspace / "important.txt").read_text(encoding="utf-8") == "keep-this"
    assert not list(workspace.parent.glob(".*.restore-stage"))


def test_snapshot_retention_limit_is_enforced(tmp_path):
    store, workspace_storage = make_store(tmp_path, max_snapshots=1)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)

    store.create(WORKSPACE_ID, workspace)
    with pytest.raises(SnapshotError, match="retention limit"):
        store.create(WORKSPACE_ID, workspace)


@pytest.mark.parametrize("name", ["../escape.txt", "src/../../escape.txt", "/etc/passwd"])
def test_archive_parent_and_absolute_paths_are_rejected(name):
    member = tarfile.TarInfo(name)
    member.type = tarfile.REGTYPE
    with pytest.raises(SnapshotError, match="unsafe|parent-directory|escapes"):
        SnapshotStore._safe_member(member)


def test_symbolic_link_escape_is_rejected():
    member = tarfile.TarInfo("src/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "../../../outside"
    with pytest.raises(SnapshotError, match="escapes"):
        SnapshotStore._safe_member(member)


def test_snapshot_ids_are_scoped_to_the_workspace(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    snapshot = store.create(WORKSPACE_ID, workspace)

    with pytest.raises(SnapshotError, match="not found"):
        store._snapshot_record("dbx_zyxwvutsrqponmlkjihgfe", snapshot["snapshot_id"])


def test_missing_key_blocks_create_and_restore_but_allows_deletion(tmp_path):
    workspace_storage = make_storage(tmp_path / "workspace-data")
    snapshot_storage = make_storage(tmp_path / "snapshot-data")
    store = SnapshotStore(snapshot_storage, workspace_storage, "")
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)

    with pytest.raises(SnapshotError, match="key is not configured"):
        store.create(WORKSPACE_ID, workspace)

    assert store.list(WORKSPACE_ID) == []
    store.delete_workspace(WORKSPACE_ID)
    assert not snapshot_storage._workspace_path(WORKSPACE_ID)[1].exists()
    store.delete_workspace(WORKSPACE_ID)
    assert not snapshot_storage._workspace_path(WORKSPACE_ID)[1].exists()


def test_snapshot_creation_rejects_symlinks_that_escape_workspace(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not archive", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)

    with pytest.raises(SnapshotError, match="symbolic link"):
        store.create(WORKSPACE_ID, workspace)


def test_interrupted_restore_recovers_previous_workspace_tree(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    _, workspace = workspace_storage._workspace_path(WORKSPACE_ID)
    backup = workspace.parent / f".{workspace.name}.restore-backup"
    stage = workspace.parent / f".{workspace.name}.restore-stage"
    backup.mkdir()
    stage.mkdir()
    (backup / "previous.txt").write_text("safe", encoding="utf-8")

    store.recover_restore(WORKSPACE_ID)

    assert (workspace / "previous.txt").read_text(encoding="utf-8") == "safe"
    assert not backup.exists()
    assert not stage.exists()


def test_interrupted_restore_discards_backup_after_new_tree_is_live(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    _, workspace = workspace_storage._workspace_path(WORKSPACE_ID)
    workspace.mkdir()
    backup = workspace.parent / f".{workspace.name}.restore-backup"
    backup.mkdir()
    (backup / "old.txt").write_text("old", encoding="utf-8")

    store.recover_restore(WORKSPACE_ID)

    assert workspace.is_dir()
    assert not backup.exists()


def test_invalid_encryption_key_does_not_block_snapshot_deletion(tmp_path):
    workspace_storage = make_storage(tmp_path / "workspace-data")
    snapshot_storage = make_storage(tmp_path / "snapshot-data")
    store = SnapshotStore(snapshot_storage, workspace_storage, "not-a-valid-key")

    with pytest.raises(SnapshotError, match="32-byte URL-safe"):
        store.create(WORKSPACE_ID, workspace_storage.ensure_workspace(WORKSPACE_ID))

    assert store.list(WORKSPACE_ID) == []
