import hashlib
import io
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


def test_snapshot_metadata_registry_migrates_read_only_to_worker_state(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "state.txt").write_text("persistent", encoding="utf-8")
    snapshot = store.create(WORKSPACE_ID, workspace)
    registry = tmp_path / "worker-state" / "snapshots.sqlite3"
    registry.parent.mkdir()
    interrupted = registry.with_name(registry.name + ".migrating")
    interrupted.write_text("partial migration", encoding="utf-8")

    migrated = SnapshotStore(
        store.storage,
        workspace_storage,
        secrets.token_urlsafe(32),
        registry_path=registry,
    )

    assert registry.is_file()
    assert not interrupted.exists()
    assert store.legacy_registry.is_file()
    assert migrated.list(WORKSPACE_ID)[0]["snapshot_id"] == snapshot["snapshot_id"]
    assert registry.stat().st_mode & 0o777 == 0o600


def test_snapshot_metadata_registry_rejects_symlink_target(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    target = tmp_path / "outside.sqlite3"
    target.write_text("not a database", encoding="utf-8")
    registry = tmp_path / "worker-state" / "snapshots.sqlite3"
    registry.parent.mkdir()
    registry.symlink_to(target)

    with pytest.raises(SnapshotError, match="unsafe"):
        SnapshotStore(
            store.storage,
            workspace_storage,
            secrets.token_urlsafe(32),
            registry_path=registry,
        )


def test_snapshot_extract_uses_group_permissions_without_chown(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "restored.txt").write_text("group writable", encoding="utf-8")
    snapshot = store.create(WORKSPACE_ID, workspace)
    stage = workspace.parent / f".{workspace.name}.restore-stage"
    workspace_storage.prepare_project_directory(
        WORKSPACE_ID, stage, workspace_storage._project_id(WORKSPACE_ID), 3
    )

    def deny_chown(*_args):
        raise PermissionError("worker must not need CAP_CHOWN")

    workspace_storage.owner_setter = deny_chown
    encrypted_path, _ = store._snapshot_record(WORKSPACE_ID, snapshot["snapshot_id"])
    store._extract(encrypted_path, WORKSPACE_ID, snapshot["snapshot_id"], stage)

    restored = stage / "restored.txt"
    assert restored.read_text(encoding="utf-8") == "group writable"
    assert restored.stat().st_mode & 0o060 == 0o060


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


def test_snapshot_can_restore_a_missing_registered_workspace(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "recovered.txt").write_text("recover me", encoding="utf-8")
    snapshot = store.create(WORKSPACE_ID, workspace)
    (workspace / "recovered.txt").unlink()
    workspace.rmdir()

    result = store.restore(WORKSPACE_ID, snapshot["snapshot_id"], workspace)

    assert result["state"] == "restored"
    assert (workspace / "recovered.txt").read_text(encoding="utf-8") == "recover me"


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
    with pytest.raises(SnapshotError, match="key is not configured"):
        store.restore(WORKSPACE_ID, "a" * 32, workspace)
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
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    workspace.rmdir()
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
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
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
    store.delete_workspace(WORKSPACE_ID)
    assert not snapshot_storage._workspace_path(WORKSPACE_ID)[1].exists()


def test_orphaned_snapshot_and_partial_archive_files_are_reconciled(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    snapshot = store.create(WORKSPACE_ID, workspace)
    snapshot_directory = store.storage._workspace_path(WORKSPACE_ID)[1]
    encrypted_file = snapshot_directory / f"{snapshot['snapshot_id']}.dbxsnap"
    temporary_file = snapshot_directory / f".{'c' * 32}.dbxsnap.tmp"
    temporary_file.write_bytes(b"partial")
    connection = store._connect()
    try:
        connection.execute(
            "DELETE FROM snapshots WHERE workspace_id = ? AND snapshot_id = ?",
            (WORKSPACE_ID, snapshot["snapshot_id"]),
        )
        connection.commit()
    finally:
        connection.close()

    assert store.list(WORKSPACE_ID) == []
    assert not encrypted_file.exists()
    assert not temporary_file.exists()


def test_missing_snapshot_metadata_registry_preserves_encrypted_files(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    snapshot = store.create(WORKSPACE_ID, workspace)
    snapshot_directory = store.storage._workspace_path(WORKSPACE_ID)[1]
    encrypted_file = snapshot_directory / f"{snapshot['snapshot_id']}.dbxsnap"
    store.registry.unlink()

    with pytest.raises(SnapshotError, match="metadata registry is missing"):
        store.list(WORKSPACE_ID)

    assert encrypted_file.is_file()


def test_missing_encrypted_file_is_detected_from_snapshot_metadata(tmp_path):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    snapshot = store.create(WORKSPACE_ID, workspace)
    encrypted_file = (
        store.storage._workspace_path(WORKSPACE_ID)[1]
        / f"{snapshot['snapshot_id']}.dbxsnap"
    )
    encrypted_file.unlink()

    assert store.list(WORKSPACE_ID) == []
    with pytest.raises(SnapshotError, match="not found"):
        store._snapshot_record(WORKSPACE_ID, snapshot["snapshot_id"])


def test_snapshot_size_limit_cleans_partial_encrypted_archive(tmp_path):
    workspace_storage = make_storage(tmp_path / "workspace-data")
    snapshot_storage = make_storage(tmp_path / "snapshot-data")
    store = SnapshotStore(
        snapshot_storage,
        workspace_storage,
        secrets.token_urlsafe(32),
        max_snapshot_bytes=128,
    )
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "large.txt").write_bytes(b"x" * 4096)

    with pytest.raises(SnapshotError, match="size limit"):
        store.create(WORKSPACE_ID, workspace)

    snapshot_directory = store.storage._workspace_path(WORKSPACE_ID)[1]
    assert not list(snapshot_directory.glob("*.dbxsnap"))
    assert not list(snapshot_directory.glob("*.dbxsnap.tmp"))
    assert store.list(WORKSPACE_ID) == []


def test_snapshot_entry_limit_is_enforced(tmp_path):
    workspace_storage = make_storage(tmp_path / "workspace-data")
    snapshot_storage = make_storage(tmp_path / "snapshot-data")
    store = SnapshotStore(
        snapshot_storage,
        workspace_storage,
        secrets.token_urlsafe(32),
        max_entries=1,
    )
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "one.txt").write_text("one", encoding="utf-8")

    with pytest.raises(SnapshotError, match="entry limit"):
        store.create(WORKSPACE_ID, workspace)

    assert store.list(WORKSPACE_ID) == []


class VerifiedArchiveReader(io.BytesIO):
    def verify(self):
        return None


def test_snapshot_restore_rejects_symlink_parent_chain(tmp_path, monkeypatch):
    store, workspace_storage = make_store(tmp_path)
    workspace = workspace_storage.ensure_workspace(WORKSPACE_ID)
    (workspace / "keep.txt").write_text("original", encoding="utf-8")
    snapshot = store.create(WORKSPACE_ID, workspace)
    encrypted_path, _ = store._snapshot_record(WORKSPACE_ID, snapshot["snapshot_id"])

    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        regular = tarfile.TarInfo("partial.txt")
        regular.size = len(b"partial")
        archive.addfile(regular, io.BytesIO(b"partial"))
        parent_link = tarfile.TarInfo("dir/sub")
        parent_link.type = tarfile.SYMTYPE
        parent_link.linkname = ".."
        archive.addfile(parent_link)
        child_link = tarfile.TarInfo("dir/sub/link")
        child_link.type = tarfile.SYMTYPE
        child_link.linkname = "../../outside"
        archive.addfile(child_link)

    payload = archive_bytes.getvalue()
    monkeypatch.setattr(
        store,
        "_decrypting_reader",
        lambda _source, _workspace_id, _snapshot_id: VerifiedArchiveReader(payload),
    )

    with pytest.raises(SnapshotError, match="nested beneath another symlink"):
        store.restore(WORKSPACE_ID, snapshot["snapshot_id"], workspace)

    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "original"
    assert not (workspace / "partial.txt").exists()
    assert not (workspace / "link").is_symlink()
    assert not list(workspace.parent.glob(".*.restore-stage"))
