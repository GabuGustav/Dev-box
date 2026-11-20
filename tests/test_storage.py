from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from worker.storage import WorkspaceStorageError, XFSWorkspaceStorage


WORKSPACE_ID = "dbx_abcdefghijklmnopqrstuv"


class FakeRunner:
    def __init__(
        self,
        filesystem="xfs rw,relatime,prjquota",
        state="Project quota state: Accounting: ON, Enforcement: ON",
        reported_target=None,
    ):
        self.filesystem = filesystem
        self.state = state
        self.reported_target = reported_target
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "findmnt":
            target = self.reported_target or args[-1]
            output = f"{target} {self.filesystem}"
        elif args[0] == "xfs_quota" and args[3] == "state -p":
            output = self.state
        else:
            output = ""
        return SimpleNamespace(stdout=output)


def make_storage(tmp_path: Path, runner: FakeRunner) -> XFSWorkspaceStorage:
    root = tmp_path / "xfs-workspaces"
    root.mkdir(parents=True)
    return XFSWorkspaceStorage(str(root), "10g", runner=runner)


def test_xfs_backend_requires_xfs_and_project_quota_enforcement(tmp_path):
    ext4 = FakeRunner(filesystem="ext4 rw,relatime")
    storage = make_storage(tmp_path, ext4)
    with pytest.raises(WorkspaceStorageError, match="XFS"):
        storage.validate_backend()

    no_quota = FakeRunner(filesystem="xfs rw,relatime")
    storage = make_storage(tmp_path / "other", no_quota)
    with pytest.raises(WorkspaceStorageError, match="project quotas"):
        storage.validate_backend()

    accounting_only = FakeRunner(state="Project quota state: Accounting: ON, Enforcement: OFF")
    storage = make_storage(tmp_path / "third", accounting_only)
    with pytest.raises(WorkspaceStorageError, match="enforcement"):
        storage.validate_backend()


def test_workspace_provisioning_sets_a_hard_project_quota(tmp_path):
    runner = FakeRunner()
    storage = make_storage(tmp_path, runner)

    directory = storage.ensure_workspace(WORKSPACE_ID)

    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    quota_commands = [call[0] for call in runner.calls if call[0][0] == "xfs_quota"]
    assert quota_commands == [
        ["xfs_quota", "-x", "-c", "state -p", str(storage.root)],
        [
            "xfs_quota",
            "-x",
            "-c",
            f"project -s -p {directory} {storage._project_id(WORKSPACE_ID)}",
            str(storage.root),
        ],
        [
            "xfs_quota",
            "-x",
            "-c",
            f"limit -p bhard=10g ihard=200000 {storage._project_id(WORKSPACE_ID)}",
            str(storage.root),
        ],
    ]


def test_workspace_project_registry_prevents_quota_id_collisions(tmp_path, monkeypatch):
    runner = FakeRunner()
    storage = make_storage(tmp_path, runner)
    monkeypatch.setattr(storage, "_project_id", lambda _: 77)
    storage.ensure_workspace(WORKSPACE_ID)

    with pytest.raises(WorkspaceStorageError, match="collision"):
        storage.ensure_workspace("dbx_zyxwvutsrqponmlkjihgfe")


def test_delete_removes_workspace_directory_and_registry_entry(tmp_path):
    runner = FakeRunner()
    storage = make_storage(tmp_path, runner)
    directory = storage.ensure_workspace(WORKSPACE_ID)
    (directory / "source.txt").write_text("workspace data", encoding="utf-8")

    storage.delete_workspace(WORKSPACE_ID)

    assert not directory.exists()
    connection = storage._connect()
    try:
        assert connection.execute(
            "SELECT workspace_id FROM workspace_projects WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone() is None
    finally:
        connection.close()


@pytest.mark.parametrize("workspace_id", ["", "dbx_short", "../dbx_abcdefghijklmnopqrstuv"])
def test_invalid_workspace_ids_are_rejected(tmp_path, workspace_id):
    storage = make_storage(tmp_path, FakeRunner())
    with pytest.raises(HTTPException) as error:
        storage.ensure_workspace(workspace_id)
    assert error.value.status_code == 422


@pytest.mark.parametrize("quota", ["0g", "-1g", "10g; rm -rf /", "10gb", ""])
def test_invalid_quota_configuration_is_rejected(tmp_path, quota):
    with pytest.raises(WorkspaceStorageError, match="positive size"):
        XFSWorkspaceStorage(str(tmp_path), quota)

def test_missing_registry_with_existing_storage_fails_closed(tmp_path):
    runner = FakeRunner()
    storage = make_storage(tmp_path, runner)
    (storage.root / "untracked-workspace-data").mkdir()

    with pytest.raises(WorkspaceStorageError, match="registry is missing"):
        storage.ensure_workspace(WORKSPACE_ID)


def test_workspace_storage_must_be_the_mountpoint_not_a_subdirectory(tmp_path):
    runner = FakeRunner(reported_target=str(tmp_path))
    storage = make_storage(tmp_path, runner)

    with pytest.raises(WorkspaceStorageError, match="dedicated mount point"):
        storage.validate_backend()


def test_workspace_storage_root_rejects_unsafe_command_characters(tmp_path):
    with pytest.raises(WorkspaceStorageError, match="unsafe command characters"):
        XFSWorkspaceStorage(str(tmp_path / "unsafe path"), "10g")
