import hashlib

import pytest
from fastapi import HTTPException

from broker import main as broker
from worker.storage import WorkspaceStorageError


class MissingContainer(Exception):
    pass


class FakeContainer:
    def __init__(self, name, workspace_id, path, status="created"):
        self.id = "container-123"
        self.name = name
        self.status = status
        self.attrs = {
            "Config": {
                "Labels": {
                    "devbox.managed": "true",
                    "devbox.workspace_id": workspace_id,
                }
            },
            "Mounts": [{"Destination": "/workspace", "Source": str(path)}],
        }
        self.started = False
        self.stopped = False
        self.removed = False
        self.paused = False

    def reload(self):
        return None

    def start(self):
        self.started = True
        self.status = "running"

    def stop(self, timeout=10):
        self.stopped = True
        self.status = "exited"

    def pause(self):
        self.paused = True
        self.status = "paused"

    def unpause(self):
        self.paused = False
        self.status = "running"

    def remove(self, force=False, v=False):
        self.removed = True


class FakeContainers:
    def __init__(self):
        self.items = {}
        self.created = []

    def get(self, name):
        if name not in self.items:
            raise MissingContainer(name)
        return self.items[name]

    def create(self, **kwargs):
        path = next(iter(kwargs["volumes"]))
        workspace_id = kwargs["labels"]["devbox.workspace_id"]
        container = FakeContainer(kwargs["name"], workspace_id, path)
        self.items[kwargs["name"]] = container
        self.created.append(kwargs)
        return container

    def list(self, all=False, filters=None):
        return list(self.items.values())


class FakeDocker:
    def __init__(self):
        self.containers = FakeContainers()


def workspace_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "WORKSPACE_STORAGE_ROOT", tmp_path)
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    digest_path = tmp_path / hashlib.sha256(workspace_id.encode()).hexdigest()[:32]
    digest_path.mkdir()
    return workspace_id, digest_path


def test_broker_auth_requires_separate_configured_token(monkeypatch):
    monkeypatch.setattr(broker, "SERVICE_TOKEN", "broker-secret-value")

    with pytest.raises(HTTPException) as missing:
        broker.require_service_token(None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as wrong:
        broker.require_service_token("Bearer wrong-secret")
    assert wrong.value.status_code == 401

    assert broker.require_service_token("Bearer broker-secret-value") is None


def test_workspace_names_reject_invalid_ids():
    with pytest.raises(HTTPException) as error:
        broker.names_for("../outside")
    assert error.value.status_code == 422


def test_workspace_path_must_be_a_real_direct_child(tmp_path, monkeypatch):
    workspace_id, path = workspace_fixture(tmp_path, monkeypatch)
    assert broker.workspace_path_for(workspace_id) == path

    path.rmdir()
    path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        broker.workspace_path_for(workspace_id)
    assert error.value.status_code == 409


def test_start_workspace_uses_only_fixed_hardened_container_configuration(
    tmp_path, monkeypatch
):
    workspace_id, path = workspace_fixture(tmp_path, monkeypatch)
    fake = FakeDocker()
    monkeypatch.setattr(broker, "docker_client", fake)
    monkeypatch.setattr(broker, "NotFound", MissingContainer)

    result = broker.start_workspace(workspace_id)

    assert result["state"] == "running"
    assert result["workspace_id"] == workspace_id
    assert len(fake.containers.created) == 1
    config = fake.containers.created[0]
    assert config["image"] == broker.IMAGE_NAME
    assert config["network_mode"] == "none"
    assert config["user"] == "10001:10001"
    assert config["read_only"] is True
    assert config["cap_drop"] == ["ALL"]
    assert config["security_opt"] == ["no-new-privileges:true"]
    assert config["mem_limit"] == broker.WORKSPACE_MEMORY
    assert config["pids_limit"] == broker.WORKSPACE_PIDS
    assert config["volumes"] == {
        str(path): {"bind": "/workspace", "mode": "rw"}
    }
    assert "pid_mode" not in config


def test_start_workspace_rejects_existing_container_with_wrong_mount(
    tmp_path, monkeypatch
):
    workspace_id, path = workspace_fixture(tmp_path, monkeypatch)
    fake = FakeDocker()
    name, _ = broker.names_for(workspace_id)
    unsafe = FakeContainer(name, workspace_id, path / "wrong", status="running")
    fake.containers.items[name] = unsafe
    monkeypatch.setattr(broker, "docker_client", fake)
    monkeypatch.setattr(broker, "NotFound", MissingContainer)

    with pytest.raises(HTTPException) as error:
        broker.start_workspace(workspace_id)

    assert error.value.status_code == 409
    assert unsafe.stopped is True
    assert fake.containers.created == []


@pytest.mark.parametrize(
    "arguments,network_enabled",
    [
        (["status", "--short", "--branch"], False),
        (["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color"], False),
        (["remote", "get-url", "origin"], False),
        (
            [
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                "main",
                "--",
                "https://github.com/example/project",
                "/workspace",
            ],
            True,
        ),
        (["fetch", "--prune", "origin"], True),
        (["switch", "--create", "feature/security"], False),
        (["add", "--", "src/main.py"], False),
        (
            [
                "-c",
                "user.name=DevBox Agent",
                "-c",
                "user.email=devbox@localhost",
                "commit",
                "-m",
                "secure commit",
            ],
            False,
        ),
    ],
)
def test_git_helper_accepts_allowlisted_operations(arguments, network_enabled):
    broker.validate_git_arguments(arguments, network_enabled)


@pytest.mark.parametrize(
    "arguments,network_enabled",
    [
        (["sh", "-c", "id"], False),
        (
            [
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                "main",
                "--",
                "http://127.0.0.1/private",
                "/workspace",
            ],
            True,
        ),
        (["status", "--short", "--branch"], True),
        (["fetch", "--prune", "origin"], False),
        (["add", "--", "../../etc/passwd"], False),
        (["switch", "--create", "../escape"], False),
    ],
)
def test_git_helper_rejects_non_allowlisted_or_unsafe_operations(
    arguments, network_enabled
):
    with pytest.raises(HTTPException) as error:
        broker.validate_git_arguments(arguments, network_enabled)
    assert error.value.status_code == 422


def test_broker_startup_rejects_placeholder_or_short_token(monkeypatch):
    monkeypatch.setattr(
        broker, "SERVICE_TOKEN", "replace-this-with-a-different-long-random-secret"
    )
    with pytest.raises(RuntimeError, match="non-placeholder"):
        broker.validate_service_token()

    monkeypatch.setattr(broker, "SERVICE_TOKEN", "too-short")
    with pytest.raises(RuntimeError, match="non-placeholder"):
        broker.validate_service_token()

    monkeypatch.setattr(broker, "SERVICE_TOKEN", "x" * 48)
    assert broker.validate_service_token() is None

def test_stop_missing_workspace_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "WORKSPACE_STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(broker, "NotFound", MissingContainer)
    monkeypatch.setattr(broker, "docker_client", FakeDocker())

    result = broker.stop_workspace("dbx_abcdefghijklmnopqrstuv")

    assert result["state"] == "absent"
    assert result["container_id"] is None


class FakeXFSStorage:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.failure = None

    def _record(self, value):
        if self.failure:
            raise self.failure
        self.calls.append(value)

    def validate_backend(self):
        self._record(("validate",))

    def _workspace_path(self, workspace_id):
        digest = hashlib.sha256(workspace_id.encode()).hexdigest()[:32]
        return digest, self.root / digest

    def _project_id(self, workspace_id):
        digest = __import__("hashlib").sha256(workspace_id.encode()).hexdigest()
        return (int(digest[:8], 16) & 0x7FFFFFFF) or 1

    def ensure_workspace(self, workspace_id):
        _, directory = self._workspace_path(workspace_id)
        directory.mkdir(exist_ok=True)
        self._record(("ensure", workspace_id))
        return directory

    def set_project_quota(self, workspace_id, multiplier=1):
        self._record(("quota", workspace_id, multiplier))

    def prepare_project_directory(
        self, workspace_id, directory, project_id, quota_multiplier=1
    ):
        self._record(("prepare", workspace_id, project_id, quota_multiplier))
        directory.mkdir()

    def mark_workspace_initialized(self, workspace_id, directory):
        self._record(("mark", workspace_id, str(directory)))

    def delete_workspace(self, workspace_id):
        self._record(("delete", workspace_id))


def test_broker_storage_api_delegates_only_workspace_scoped_operations(
    tmp_path, monkeypatch
):
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    workspace = FakeXFSStorage(tmp_path / "workspaces")
    snapshot = FakeXFSStorage(tmp_path / "snapshots")
    workspace.root.mkdir()
    snapshot.root.mkdir()
    monkeypatch.setattr(
        broker, "storage_backends", {"workspace": workspace, "snapshot": snapshot}
    )

    assert broker.api_validate_storage("workspace") == {
        "storage_kind": "workspace",
        "status": "ready",
    }
    ensured = broker.api_ensure_storage_workspace("workspace", workspace_id)
    expected_directory = workspace._workspace_path(workspace_id)[1]
    assert ensured["path"] == str(expected_directory)

    quota = broker.api_set_storage_quota(
        "workspace", workspace_id, broker.QuotaRequest(multiplier=3)
    )
    assert quota["multiplier"] == 3

    prepared = broker.api_prepare_restore_directory(
        "workspace", workspace_id, broker.QuotaRequest(multiplier=3)
    )
    assert prepared["path"].endswith(".restore-stage")

    marked = broker.api_mark_storage_initialized("workspace", workspace_id)
    assert marked["status"] == "initialized"
    deleted = broker.api_delete_storage_workspace("workspace", workspace_id)
    assert deleted["status"] == "deleted"
    assert [call[0] for call in workspace.calls] == [
        "validate",
        "ensure",
        "validate",
        "quota",
        "validate",
        "prepare",
        "validate",
        "mark",
        "delete",
    ]
    assert snapshot.calls == []


def test_broker_storage_api_maps_storage_failures_to_service_unavailable(
    tmp_path, monkeypatch
):
    storage = FakeXFSStorage(tmp_path)
    storage.failure = WorkspaceStorageError("XFS unavailable")
    monkeypatch.setattr(
        broker, "storage_backends", {"workspace": storage, "snapshot": storage}
    )

    with pytest.raises(HTTPException) as error:
        broker.api_validate_storage("workspace")
    assert error.value.status_code == 503


class FakeRestoreStorage:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def validate_backend(self):
        return None

    def _workspace_path(self, workspace_id):
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
        return digest, self.root / digest

    def mark_workspace_initialized(self, workspace_id, directory):
        self.calls.append(("mark", workspace_id, directory))

    def set_project_quota(self, workspace_id, multiplier=1):
        self.calls.append(("quota", workspace_id, multiplier))


def test_broker_commit_restore_performs_and_registers_directory_swap(tmp_path, monkeypatch):
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    storage = FakeRestoreStorage(tmp_path)
    monkeypatch.setattr(broker, "storage_backends", {"workspace": storage, "snapshot": storage})
    _, workspace = storage._workspace_path(workspace_id)
    workspace.mkdir()
    (workspace / "old.txt").write_text("old", encoding="utf-8")
    stage = tmp_path / f".{workspace.name}.restore-stage"
    stage.mkdir()
    (stage / "new.txt").write_text("new", encoding="utf-8")

    result = broker.api_commit_storage_restore("workspace", workspace_id)

    assert result["state"] == "restored"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (workspace / "old.txt").exists()
    assert not stage.exists()
    assert not (tmp_path / f".{workspace.name}.restore-backup").exists()
    assert ("mark", workspace_id, workspace) in storage.calls
    assert ("quota", workspace_id, 1) in storage.calls


def test_broker_recovery_restores_backup_and_discards_stage(tmp_path, monkeypatch):
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    storage = FakeRestoreStorage(tmp_path)
    monkeypatch.setattr(broker, "storage_backends", {"workspace": storage, "snapshot": storage})
    _, workspace = storage._workspace_path(workspace_id)
    backup = tmp_path / f".{workspace.name}.restore-backup"
    backup.mkdir()
    (backup / "previous.txt").write_text("safe", encoding="utf-8")
    stage = tmp_path / f".{workspace.name}.restore-stage"
    stage.mkdir()

    result = broker.api_recover_storage_restore("workspace", workspace_id)

    assert result["state"] == "recovered"
    assert (workspace / "previous.txt").read_text(encoding="utf-8") == "safe"
    assert not backup.exists()
    assert not stage.exists()
    assert ("mark", workspace_id, workspace) in storage.calls
    assert ("quota", workspace_id, 1) in storage.calls


def test_broker_initializes_worker_state_with_restricted_permissions(tmp_path, monkeypatch):
    state = tmp_path / "worker-state"
    monkeypatch.setattr(broker, "WORKER_STATE_ROOT", state)
    monkeypatch.setattr(broker.os, "chown", lambda *_args: None)

    broker.prepare_worker_state()

    assert state.is_dir()
    assert state.stat().st_mode & 0o777 == 0o700
