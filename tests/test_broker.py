import hashlib

import pytest
from fastapi import HTTPException

from broker import main as broker


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
