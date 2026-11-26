import hashlib
import shutil

import pytest

from worker.broker_client import BrokerUnavailable
from worker.remote_storage import RemoteWorkspaceStorage
from worker.storage import WorkspaceStorageError

WORKSPACE_ID = "dbx_abcdefghijklmnopqrstuv"


class FakeStorageBroker:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.failure = None

    def _record(self, call):
        if self.failure:
            raise self.failure
        self.calls.append(call)

    def validate_storage(self, kind):
        self._record(("validate", kind))
        return {"status": "ready"}

    def ensure_storage_workspace(self, kind, workspace_id):
        self._record(("ensure", kind, workspace_id))
        digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:32]
        directory = self.root / digest
        directory.mkdir(exist_ok=True)
        return {"path": str(directory)}

    def set_storage_quota(self, kind, workspace_id, multiplier):
        self._record(("quota", kind, workspace_id, multiplier))
        return {"multiplier": multiplier}

    def prepare_restore_directory(self, kind, workspace_id, multiplier):
        self._record(("prepare", kind, workspace_id, multiplier))
        digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:32]
        stage = self.root / f".{digest}.restore-stage"
        stage.mkdir()
        return {"path": str(stage)}

    def mark_storage_initialized(self, kind, workspace_id):
        self._record(("mark", kind, workspace_id))
        return {"status": "initialized"}

    def recover_storage_restore(self, kind, workspace_id):
        self._record(("recover", kind, workspace_id))
        return {"state": "recovered"}

    def commit_storage_restore(self, kind, workspace_id):
        self._record(("commit", kind, workspace_id))
        return {"state": "restored"}

    def delete_storage_workspace(self, kind, workspace_id):
        self._record(("delete", kind, workspace_id))
        digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:32]
        directory = self.root / digest
        if directory.exists():
            shutil.rmtree(directory)
        return {"status": "deleted"}


def make_storage(tmp_path, kind="workspace"):
    root = tmp_path / kind
    root.mkdir()
    broker = FakeStorageBroker(root)
    storage = RemoteWorkspaceStorage(
        str(root),
        "10g",
        200000,
        kind,
        broker,
    )
    return storage, broker, root


def test_remote_storage_validates_backend_through_broker(tmp_path):
    storage, broker, _ = make_storage(tmp_path, "snapshot")

    storage.validate_backend()

    assert broker.calls == [("validate", "snapshot")]


def test_remote_storage_ensures_directory_through_broker(tmp_path):
    storage, broker, root = make_storage(tmp_path)

    directory = storage.ensure_workspace(WORKSPACE_ID)

    digest = hashlib.sha256(WORKSPACE_ID.encode()).hexdigest()[:32]
    assert directory == root / digest
    assert directory.is_dir()
    assert broker.calls == [("ensure", "workspace", WORKSPACE_ID)]


def test_remote_storage_sets_bounded_quota_through_broker(tmp_path):
    storage, broker, _ = make_storage(tmp_path)

    storage.set_project_quota(WORKSPACE_ID, multiplier=3)

    assert broker.calls == [("quota", "workspace", WORKSPACE_ID, 3)]
    with pytest.raises(WorkspaceStorageError, match="between 1 and 10"):
        storage.set_project_quota(WORKSPACE_ID, multiplier=11)


def test_remote_storage_prepares_only_expected_restore_directory(tmp_path):
    storage, broker, root = make_storage(tmp_path)
    _, workspace_directory = storage._workspace_path(WORKSPACE_ID)
    stage = root / f".{workspace_directory.name}.restore-stage"

    storage.prepare_project_directory(
        WORKSPACE_ID,
        stage,
        storage._project_id(WORKSPACE_ID),
        quota_multiplier=3,
    )

    assert stage.is_dir()
    assert broker.calls == [("prepare", "workspace", WORKSPACE_ID, 3)]


def test_remote_storage_rejects_wrong_restore_path_or_project_id(tmp_path):
    storage, broker, root = make_storage(tmp_path)
    _, workspace_directory = storage._workspace_path(WORKSPACE_ID)
    stage = root / f".{workspace_directory.name}.restore-stage"

    with pytest.raises(WorkspaceStorageError, match="staging path"):
        storage.prepare_project_directory(
            WORKSPACE_ID,
            root / "arbitrary",
            storage._project_id(WORKSPACE_ID),
        )
    with pytest.raises(WorkspaceStorageError, match="project ID"):
        storage.prepare_project_directory(
            WORKSPACE_ID,
            stage,
            storage._project_id(WORKSPACE_ID) + 1,
        )
    assert broker.calls == []


def test_remote_storage_marks_and_deletes_workspace_through_broker(tmp_path):
    storage, broker, _ = make_storage(tmp_path)
    directory = storage.ensure_workspace(WORKSPACE_ID)

    storage.mark_workspace_initialized(WORKSPACE_ID, directory)
    storage.delete_workspace(WORKSPACE_ID)

    assert broker.calls[-2:] == [
        ("mark", "workspace", WORKSPACE_ID),
        ("delete", "workspace", WORKSPACE_ID),
    ]
    assert not directory.exists()


def test_remote_storage_fails_closed_when_broker_is_unavailable(tmp_path):
    storage, broker, _ = make_storage(tmp_path)
    broker.failure = BrokerUnavailable("offline")

    with pytest.raises(WorkspaceStorageError, match="broker storage operation failed"):
        storage.validate_backend()


def test_remote_storage_delegates_restore_swap_and_recovery(tmp_path):
    storage, broker, _ = make_storage(tmp_path)

    storage.commit_restore(WORKSPACE_ID)
    storage.recover_restore(WORKSPACE_ID)

    assert broker.calls == [
        ("commit", "workspace", WORKSPACE_ID),
        ("recover", "workspace", WORKSPACE_ID),
    ]


def test_remote_storage_rejects_invalid_workspace_before_restore_rpc(tmp_path):
    storage, broker, _ = make_storage(tmp_path)

    with pytest.raises(WorkspaceStorageError, match="Invalid workspace ID"):
        storage.commit_restore("../invalid")
    with pytest.raises(WorkspaceStorageError, match="Invalid workspace ID"):
        storage.recover_restore("../invalid")

    assert broker.calls == []
