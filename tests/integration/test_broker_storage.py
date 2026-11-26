import errno
import os
import shutil
import subprocess
import sys
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


def run_as(uid: int, gid: int, script: str, *arguments: str) -> subprocess.CompletedProcess:
    def drop_identity() -> None:
        os.setgroups([gid])
        os.setgid(gid)
        os.setuid(uid)

    return subprocess.run(
        [sys.executable, "-c", script, *arguments],
        check=False,
        capture_output=True,
        text=True,
        preexec_fn=drop_identity,
    )


def make_backends():
    BROKER_STATE_ROOT.mkdir(parents=True, exist_ok=True)
    workspace = XFSWorkspaceStorage(
        WORKSPACE_ROOT,
        "16m",
        inode_limit=1000,
        registry_path=BROKER_STATE_ROOT / "workspace-projects.sqlite3",
    )
    snapshot = XFSWorkspaceStorage(
        SNAPSHOT_ROOT,
        "32m",
        inode_limit=2000,
        registry_path=BROKER_STATE_ROOT / "snapshot-projects.sqlite3",
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

    try:
        assert broker.api_validate_storage("workspace")["status"] == "ready"
        assert broker.api_validate_storage("snapshot")["status"] == "ready"
        broker.prepare_shared_storage_permissions(workspace_storage)
        broker.prepare_shared_storage_permissions(snapshot_storage)

        workspace_path = Path(
            broker.api_ensure_storage_workspace("workspace", workspace_id)["path"]
        )
        assert workspace_path.stat().st_gid == 10001
        assert workspace_path.stat().st_mode & 0o2070 == 0o2070
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

        assert workspace_path.stat().st_gid == 10001
        assert workspace_path.stat().st_mode & 0o2070 == 0o2070
        worker_write = workspace_path / "worker-created.txt"
        worker_result = run_as(
            10002, 10001, "import sys; open(sys.argv[1], 'w').write('worker')", str(worker_write)
        )
        assert worker_result.returncode == 0, worker_result.stderr
        runtime_result = run_as(
            10001, 10001, "import sys; open(sys.argv[1], 'a').write('-runtime')", str(worker_write)
        )
        assert runtime_result.returncode == 0, runtime_result.stderr
        assert worker_write.read_text(encoding="utf-8") == "worker-runtime"
        denied = run_as(
            10002,
            10001,
            "import sys; p=sys.argv[1]; "
            "f=open(p, 'x') if False else None; "
            "sys.exit(0 if not __import__('os').access(__import__('os').path.dirname(p), 2) else 1)",
            str(Path(WORKSPACE_ROOT) / ".worker-root-write-probe"),
        )
        assert denied.returncode == 0, denied.stderr

        prepared = broker.api_prepare_restore_directory(
            "workspace", workspace_id, broker.QuotaRequest(multiplier=2)
        )
        stage = Path(prepared["path"])
        assert stage.is_dir()
        staged_file = stage / "restored.txt"
        stage_write = run_as(
            10002, 10001, "import sys,os; p=sys.argv[1]; open(p, 'w').write('restored'); os.chmod(p, 0o660)", str(staged_file)
        )
        assert stage_write.returncode == 0, stage_write.stderr
        broker.api_commit_storage_restore("workspace", workspace_id)
        assert not stage.exists()
        assert (workspace_path / "restored.txt").read_text(encoding="utf-8") == "restored"
        runtime_after_restore = run_as(
            10001, 10001, "import sys; open(sys.argv[1], 'a').write('-runtime')", str(workspace_path / "restored.txt")
        )
        assert runtime_after_restore.returncode == 0, runtime_after_restore.stderr
        assert (workspace_path / "restored.txt").read_text(encoding="utf-8") == "restored-runtime"

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
        broker.api_delete_storage_workspace("workspace", workspace_id)
        broker.api_delete_storage_workspace("snapshot", snapshot_id)
