import pytest
from fastapi import HTTPException

from worker import main as executor


class MissingResource(Exception):
    pass


class FakeVolume:
    def __init__(self, name):
        self.name = name


class FakeContainer:
    id = "container-123"

    def __init__(self):
        self.status = "created"
        self.create_kwargs = None
        self.started = False
        self.attrs = {"Mounts": []}

    def reload(self):
        return None

    def start(self):
        self.started = True
        self.status = "running"

    def unpause(self):
        self.status = "running"


class FakeVolumes:
    def __init__(self):
        self.created = []

    def get(self, name):
        raise MissingResource(name)

    def create(self, **kwargs):
        volume = FakeVolume(kwargs["name"])
        self.created.append((volume, kwargs))
        return volume


class FakeContainers:
    def __init__(self):
        self.created = []
        self.existing = None

    def get(self, name):
        if self.existing is not None:
            return self.existing
        raise MissingResource(name)

    def create(self, **kwargs):
        container = FakeContainer()
        container.create_kwargs = kwargs
        self.created.append((container, kwargs))
        return container


class FakeStorage:
    def __init__(self, path):
        self.path = path
        self.deleted = []
        self.ensured = []

    def ensure_workspace(self, workspace_id):
        self.ensured.append(workspace_id)
        return self.path

    def delete_workspace(self, workspace_id):
        self.deleted.append(workspace_id)


class FakeDockerClient:
    def __init__(self):
        self.volumes = FakeVolumes()
        self.containers = FakeContainers()


class FakeSnapshotStore:
    def recover_restore(self, workspace_id):
        return None

    def delete_workspace(self, workspace_id):
        return None


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


def test_start_creates_hardened_container_on_workspace_storage(monkeypatch, tmp_path):
    fake = FakeDockerClient()
    monkeypatch.setattr(executor, "docker_client", fake)
    monkeypatch.setattr(executor, "NotFound", MissingResource)
    monkeypatch.setattr(executor, "IMAGE_NAME", "devbox-workspace:test")
    monkeypatch.setattr(executor, "WORKSPACE_CPU", 1.5)
    monkeypatch.setattr(executor, "WORKSPACE_MEMORY", "768m")
    monkeypatch.setattr(executor, "WORKSPACE_PIDS", 128)
    storage_path = tmp_path / "workspace"
    storage_path.mkdir()
    monkeypatch.setattr(executor, "workspace_storage", FakeStorage(storage_path))
    monkeypatch.setattr(executor, "snapshot_store", FakeSnapshotStore())

    result = executor.start_workspace("dbx_abcdefghijklmnopqrstuv")

    assert result["state"] == "running"
    assert result["container_id"] == "container-123"
    assert result["storage_ref"].startswith("xfs-")
    assert len(fake.containers.created) == 1

    container = fake.containers.created[0][1]
    assert container["user"] == "10001:10001"
    assert container["network_mode"] == "none"
    assert container["pid_mode"] == "private"
    assert container["ipc_mode"] == "private"
    assert container["read_only"] is True
    assert container["cap_drop"] == ["ALL"]
    assert container["security_opt"] == ["no-new-privileges:true"]
    assert container["mem_limit"] == "768m"
    assert container["nano_cpus"] == 1_500_000_000
    assert container["pids_limit"] == 128
    assert container["restart_policy"] == {"Name": "unless-stopped"}
    assert container["volumes"] == {
        str(storage_path): {"bind": "/workspace", "mode": "rw"}
    }


def test_internal_service_requires_configured_token(monkeypatch):
    monkeypatch.setattr(executor, "SERVICE_TOKEN", "test-secret")

    with pytest.raises(HTTPException) as error:
        executor.require_service_token("Bearer wrong-secret")
    assert error.value.status_code == 401

    assert executor.require_service_token("Bearer test-secret") is None


def test_resume_reapplies_quota_and_validates_xfs_mount(monkeypatch, tmp_path):
    fake = FakeDockerClient()
    monkeypatch.setattr(executor, "docker_client", fake)
    monkeypatch.setattr(executor, "NotFound", MissingResource)
    storage_path = tmp_path / "workspace"
    storage_path.mkdir()
    storage = FakeStorage(storage_path)
    monkeypatch.setattr(executor, "workspace_storage", storage)
    monkeypatch.setattr(executor, "snapshot_store", FakeSnapshotStore())
    existing = FakeContainer()
    existing.status = "exited"
    existing.attrs = {
        "Mounts": [{"Destination": "/workspace", "Source": str(storage_path)}]
    }
    fake.containers.existing = existing

    result = executor.resume_workspace("dbx_abcdefghijklmnopqrstuv")

    assert result["state"] == "running"
    assert result["storage_ref"].startswith("xfs-")
    assert existing.started is True


def test_start_rejects_existing_container_with_legacy_mount(monkeypatch, tmp_path):
    fake = FakeDockerClient()
    monkeypatch.setattr(executor, "docker_client", fake)
    monkeypatch.setattr(executor, "NotFound", MissingResource)
    storage_path = tmp_path / "workspace"
    storage_path.mkdir()
    storage = FakeStorage(storage_path)
    monkeypatch.setattr(executor, "workspace_storage", storage)
    monkeypatch.setattr(executor, "snapshot_store", FakeSnapshotStore())
    existing = FakeContainer()
    existing.status = "exited"
    fake.containers.existing = existing

    with pytest.raises(HTTPException) as error:
        executor.start_workspace("dbx_abcdefghijklmnopqrstuv")

    assert error.value.status_code == 409
    assert existing.started is False
    assert storage.ensured == ["dbx_abcdefghijklmnopqrstuv"]


def test_resume_rejects_existing_container_with_legacy_mount(monkeypatch, tmp_path):
    fake = FakeDockerClient()
    monkeypatch.setattr(executor, "docker_client", fake)
    monkeypatch.setattr(executor, "NotFound", MissingResource)
    storage_path = tmp_path / "workspace"
    storage_path.mkdir()
    storage = FakeStorage(storage_path)
    monkeypatch.setattr(executor, "workspace_storage", storage)
    monkeypatch.setattr(executor, "snapshot_store", FakeSnapshotStore())
    existing = FakeContainer()
    existing.status = "exited"
    fake.containers.existing = existing

    with pytest.raises(HTTPException) as error:
        executor.resume_workspace("dbx_abcdefghijklmnopqrstuv")

    assert error.value.status_code == 409
    assert existing.started is False
    assert storage.ensured == ["dbx_abcdefghijklmnopqrstuv"]


class ReconcileContainer:
    def __init__(self, name, workspace_id, source, status="running"):
        self.id = f"id-{name}"
        self.name = name
        self.status = status
        self.attrs = {
            "Config": {
                "Labels": {
                    "devbox.managed": "true",
                    "devbox.workspace_id": workspace_id,
                }
            },
            "Mounts": [{"Destination": "/workspace", "Source": source}],
        }
        self.stopped = False

    def reload(self):
        return None

    def stop(self, timeout):
        self.stopped = True
        self.status = "exited"

    def unpause(self):
        self.status = "running"


class ReconcileContainers:
    def __init__(self, containers):
        self.items = containers

    def list(self, **kwargs):
        assert kwargs == {"all": True, "filters": {"label": "devbox.managed=true"}}
        return self.items


class ReconcileDockerClient:
    def __init__(self, containers):
        self.containers = ReconcileContainers(containers)


def test_reconciliation_stops_only_containers_with_invalid_workspace_mounts(
    monkeypatch, tmp_path
):
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    valid = ReconcileContainer(
        executor.names_for(workspace_id)[0], workspace_id, str(tmp_path / "workspace")
    )
    invalid = ReconcileContainer(
        "devbox-ws-wrong", workspace_id, str(tmp_path / "wrong-workspace")
    )
    monkeypatch.setattr(executor, "docker_client", ReconcileDockerClient([valid, invalid]))

    class Storage:
        def ensure_workspace(self, requested_id):
            assert requested_id == workspace_id
            return tmp_path / "workspace"

    monkeypatch.setattr(executor, "workspace_storage", Storage())
    monkeypatch.setattr(executor, "snapshot_store", FakeSnapshotStore())

    stopped = executor.reconcile_managed_containers()

    assert stopped == 1
    assert valid.stopped is False
    assert invalid.stopped is True


def test_reconciliation_stops_container_when_workspace_storage_is_unavailable(monkeypatch):
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    container = ReconcileContainer(
        executor.names_for(workspace_id)[0], workspace_id, "/srv/devbox/workspaces/path"
    )
    monkeypatch.setattr(executor, "docker_client", ReconcileDockerClient([container]))

    class UnavailableStorage:
        def ensure_workspace(self, requested_id):
            raise RuntimeError("XFS mount unavailable")

    monkeypatch.setattr(executor, "workspace_storage", UnavailableStorage())
    monkeypatch.setattr(executor, "snapshot_store", FakeSnapshotStore())

    stopped = executor.reconcile_managed_containers()

    assert stopped == 1
    assert container.stopped is True
