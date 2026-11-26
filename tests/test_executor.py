import hashlib

import pytest
from fastapi import HTTPException

from worker import main as executor
from worker.broker_client import BrokerNotFound


class FakeBrokerContainer:
    def __init__(self, name, workspace_id, source, status="created"):
        self.id = f"container-{workspace_id}"
        self.name = name
        self.status = status
        self.stopped = False
        self.started = False
        self.attrs = {
            "Config": {
                "Labels": {
                    "devbox.managed": "true",
                    "devbox.workspace_id": workspace_id,
                }
            },
            "Mounts": [{"Destination": "/workspace", "Source": source}],
        }

    def reload(self):
        return None

    def start(self):
        self.started = True
        self.status = "running"

    def stop(self, timeout=10):
        self.stopped = True
        self.status = "exited"

    def pause(self):
        self.status = "paused"

    def unpause(self):
        self.status = "running"


class FakeBrokerClient:
    def __init__(self, workspace_path):
        self.workspace_path = workspace_path
        self.containers = {}
        self.started = []
        self.stopped = []
        self.paused = []
        self.resumed = []
        self.deleted = []
        self.git_calls = []
        self.reconcile_calls = 0

    def ping(self):
        return {"status": "ready"}

    def validate_config(self):
        return None

    def get_workspace(self, workspace_id):
        if workspace_id not in self.containers:
            raise BrokerNotFound("absent")
        return self.containers[workspace_id]

    def start_workspace(self, workspace_id):
        container = FakeBrokerContainer(
            executor.names_for(workspace_id)[0],
            workspace_id,
            str(self.workspace_path),
            status="running",
        )
        self.containers[workspace_id] = container
        self.started.append(workspace_id)
        return container

    def stop_workspace(self, workspace_id):
        container = self.containers.get(workspace_id)
        if container:
            container.stop()
        self.stopped.append(workspace_id)
        return {
            "workspace_id": workspace_id,
            "container_id": container.id if container else None,
            "state": container.status if container else "absent",
        }

    def pause_workspace(self, workspace_id):
        container = self.containers[workspace_id]
        container.pause()
        self.paused.append(workspace_id)
        return {"workspace_id": workspace_id, "state": container.status}

    def resume_workspace(self, workspace_id):
        container = self.containers[workspace_id]
        container.start()
        self.resumed.append(workspace_id)
        return {"workspace_id": workspace_id, "state": container.status}

    def delete_workspace(self, workspace_id):
        self.deleted.append(workspace_id)
        self.containers.pop(workspace_id, None)
        return {"workspace_id": workspace_id, "state": "deleted"}

    def list_managed_workspace_ids(self):
        return list(self.containers)

    def reconcile_git_helpers(self):
        self.reconcile_calls += 1
        return 2

    def run_git_helper(self, workspace_id, arguments, *, network_enabled=False):
        self.git_calls.append((workspace_id, arguments, network_enabled))
        return "git operation complete"


class FakeStorage:
    def __init__(self, path, validation_error=None):
        self.path = path
        self.validation_error = validation_error
        self.deleted = []
        self.ensured = []
        self.validated = []

    def ensure_workspace(self, workspace_id):
        self.ensured.append(workspace_id)
        return self.path

    def validate_backend(self):
        self.validated.append(True)
        if self.validation_error:
            raise self.validation_error

    def _workspace_path(self, workspace_id):
        digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:32]
        return digest, self.path

    def delete_workspace(self, workspace_id):
        self.deleted.append(workspace_id)


class FakeSnapshotStore:
    def __init__(self):
        self.recovered = []
        self.deleted = []
        self.created = []

    def recover_restore(self, workspace_id):
        self.recovered.append(workspace_id)

    def delete_workspace(self, workspace_id):
        self.deleted.append(workspace_id)

    def create(self, workspace_id, workspace_path):
        self.created.append(workspace_id)
        return {"workspace_id": workspace_id, "snapshot_id": "snap-1"}

    def restore(self, workspace_id, snapshot_id, workspace_path):
        return {"workspace_id": workspace_id, "snapshot_id": snapshot_id}

    def list(self, workspace_id):
        return []

    def delete(self, workspace_id, snapshot_id):
        return None


def configure_worker(monkeypatch, tmp_path):
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    storage_path = tmp_path / "workspace"
    storage_path.mkdir()
    storage = FakeStorage(storage_path)
    snapshots = FakeSnapshotStore()
    broker = FakeBrokerClient(storage_path)
    monkeypatch.setattr(executor, "workspace_storage", storage)
    monkeypatch.setattr(executor, "snapshot_store", snapshots)
    monkeypatch.setattr(executor, "broker_client", broker)
    return workspace_id, storage_path, storage, snapshots, broker


def test_workspace_resource_names_are_stable_and_scoped():
    first = executor.names_for("dbx_abcdefghijklmnopqrstuv")
    second = executor.names_for("dbx_abcdefghijklmnopqrstuv")
    other = executor.names_for("dbx_zyxwvutsrqponmlkjihgfe")

    assert first == second
    assert first != other
    assert first[0].startswith("devbox-ws-")
    assert first[1].startswith("xfs-")


@pytest.mark.parametrize("workspace_id", ["", "dbx_short", "../dbx_abcdefghijklmnopqrstuv"])
def test_invalid_workspace_ids_are_rejected(workspace_id):
    with pytest.raises(HTTPException) as error:
        executor.names_for(workspace_id)
    assert error.value.status_code == 422


def test_start_delegates_workspace_creation_to_broker(monkeypatch, tmp_path):
    workspace_id, storage_path, storage, snapshots, broker = configure_worker(
        monkeypatch, tmp_path
    )

    result = executor.start_workspace(workspace_id)

    assert result["state"] == "running"
    assert result["container_id"] == f"container-{workspace_id}"
    assert result["storage_ref"].startswith("xfs-")
    assert broker.started == [workspace_id]
    assert storage.ensured == [workspace_id]
    assert snapshots.recovered == [workspace_id]
    assert broker.containers[workspace_id].attrs["Mounts"][0]["Source"] == str(storage_path)


def test_start_fails_closed_on_unexpected_broker_mount(monkeypatch, tmp_path):
    workspace_id, _, _, _, broker = configure_worker(monkeypatch, tmp_path)
    unsafe = FakeBrokerContainer(
        executor.names_for(workspace_id)[0],
        workspace_id,
        str(tmp_path / "wrong"),
        status="running",
    )
    broker.containers[workspace_id] = unsafe

    with pytest.raises(HTTPException) as error:
        executor.start_workspace(workspace_id)

    assert error.value.status_code == 409
    assert unsafe.stopped is True
    assert broker.started == []


def test_resume_delegates_to_broker_after_storage_recovery(monkeypatch, tmp_path):
    workspace_id, _, storage, snapshots, broker = configure_worker(monkeypatch, tmp_path)
    broker.start_workspace(workspace_id)

    result = executor.resume_workspace(workspace_id)

    assert result["state"] == "running"
    assert broker.resumed == [workspace_id]
    assert storage.ensured == [workspace_id]
    assert snapshots.recovered == [workspace_id]


def test_stop_is_idempotent_and_delegated(monkeypatch, tmp_path):
    workspace_id, _, _, _, broker = configure_worker(monkeypatch, tmp_path)

    result = executor.stop_workspace(workspace_id)

    assert result["state"] == "absent"
    assert broker.stopped == [workspace_id]


def test_pause_delegates_to_broker(monkeypatch, tmp_path):
    workspace_id, _, _, _, broker = configure_worker(monkeypatch, tmp_path)
    broker.start_workspace(workspace_id)

    result = executor.pause_workspace(workspace_id)

    assert result["state"] == "paused"
    assert broker.paused == [workspace_id]


def test_snapshot_pauses_and_resumes_running_workspace(monkeypatch, tmp_path):
    workspace_id, _, _, snapshots, broker = configure_worker(monkeypatch, tmp_path)
    container = broker.start_workspace(workspace_id)

    result = executor.create_snapshot(workspace_id)

    assert result["snapshot_id"] == "snap-1"
    assert snapshots.created == [workspace_id]
    assert broker.paused == [workspace_id]
    assert container.status == "running"


def test_delete_workspace_delegates_container_and_storage_cleanup(monkeypatch, tmp_path):
    workspace_id, _, storage, snapshots, broker = configure_worker(monkeypatch, tmp_path)
    broker.start_workspace(workspace_id)

    result = executor.delete_workspace(workspace_id)

    assert result["state"] == "deleted"
    assert broker.deleted == [workspace_id]
    assert snapshots.deleted == [workspace_id]
    assert storage.deleted == [workspace_id]


def test_reconciliation_recovers_snapshot_transactions(monkeypatch, tmp_path):
    workspace_id, _, storage, snapshots, broker = configure_worker(monkeypatch, tmp_path)
    broker.start_workspace(workspace_id)

    recovered = executor.reconcile_managed_containers()

    assert recovered == 1
    assert snapshots.recovered == [workspace_id]
    assert storage.validated == [True]


def test_reconciliation_stops_workspace_when_storage_is_unavailable(monkeypatch, tmp_path):
    workspace_id, _, _, _, broker = configure_worker(monkeypatch, tmp_path)
    broker.start_workspace(workspace_id)
    monkeypatch.setattr(
        executor,
        "workspace_storage",
        FakeStorage(tmp_path / "workspace", validation_error=RuntimeError("XFS unavailable")),
    )

    recovered = executor.reconcile_managed_containers()

    assert recovered == 0
    assert broker.stopped == [workspace_id]


def test_workspace_status_reports_broker_state(monkeypatch, tmp_path):
    workspace_id, _, _, _, broker = configure_worker(monkeypatch, tmp_path)
    container = broker.start_workspace(workspace_id)

    result = executor.get_workspace_status(workspace_id)

    assert result == {
        "workspace_id": workspace_id,
        "container_id": container.id,
        "storage_ref": executor.names_for(workspace_id)[1],
        "state": "running",
    }


def test_workspace_status_fails_closed_on_invalid_broker_mount(monkeypatch, tmp_path):
    workspace_id, _, _, _, broker = configure_worker(monkeypatch, tmp_path)
    container = FakeBrokerContainer(
        executor.names_for(workspace_id)[0],
        workspace_id,
        str(tmp_path / "wrong"),
        status="running",
    )
    broker.containers[workspace_id] = container

    result = executor.get_workspace_status(workspace_id)

    assert result["state"] == "failed"
    assert result["container_id"] is None
    assert container.stopped is True


def test_workspace_status_reports_absent_container(monkeypatch, tmp_path):
    workspace_id, _, _, _, _ = configure_worker(monkeypatch, tmp_path)

    result = executor.get_workspace_status(workspace_id)

    assert result["state"] == "absent"
    assert result["container_id"] is None


def test_git_helper_calls_are_delegated_to_broker(monkeypatch, tmp_path):
    workspace_id, _, _, _, broker = configure_worker(monkeypatch, tmp_path)

    output = executor.run_git_command(
        workspace_id,
        ["status", "--short", "--branch"],
    )

    assert output == "git operation complete"
    assert broker.git_calls == [
        (workspace_id, ["status", "--short", "--branch"], False)
    ]


def test_internal_service_requires_configured_token(monkeypatch):
    monkeypatch.setattr(executor, "SERVICE_TOKEN", "test-secret")

    with pytest.raises(HTTPException) as error:
        executor.require_service_token("Bearer wrong-secret")
    assert error.value.status_code == 401

    assert executor.require_service_token("Bearer test-secret") is None
