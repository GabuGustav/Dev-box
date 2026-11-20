from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from fastapi import HTTPException

WORKSPACE_ID_PATTERN = re.compile(r"^dbx_[A-Za-z0-9_-]{20,40}$")
QUOTA_PATTERN = re.compile(r"^[1-9][0-9]*(?:[kKmMgGtT])?$")


class WorkspaceStorageError(RuntimeError):
    """Raised when the configured storage backend cannot enforce its policy."""


class XFSWorkspaceStorage:
    """Persistent workspace directories with kernel-enforced XFS project quotas.

    The storage root must be a dedicated XFS mount with project quota accounting
    and enforcement enabled. The worker must have permission to issue xfs_quota
    ioctls against that mount.
    """

    def __init__(
        self,
        root: str,
        quota_limit: str,
        inode_limit: int = 200_000,
        *,
        runner=subprocess.run,
    ) -> None:
        if not Path(root).is_absolute():
            raise WorkspaceStorageError("Workspace storage root must be an absolute path.")
        if not QUOTA_PATTERN.fullmatch(quota_limit):
            raise WorkspaceStorageError("Workspace disk quota must be a positive size such as 10g.")
        self.root = Path(root)
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", str(self.root)):
            raise WorkspaceStorageError("Workspace storage root contains unsafe command characters.")
        if inode_limit < 1:
            raise WorkspaceStorageError("Workspace inode limit must be a positive integer.")
        self.quota_limit = quota_limit.lower()
        self.inode_limit = inode_limit
        self.runner = runner
        self.registry = self.root / ".devbox-storage.sqlite3"

    def _run(self, *args: str) -> str:
        try:
            result = self.runner(
                list(args),
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspaceStorageError(f"Storage command failed: {args[0]}") from exc
        return result.stdout

    def validate_backend(self) -> None:
        if (
            not self.root.exists()
            or not self.root.is_dir()
            or self.root.is_symlink()
        ):
            raise WorkspaceStorageError("Workspace storage mount is missing or unsafe.")
        filesystem = self._run(
            "findmnt", "-n", "-o", "TARGET,FSTYPE,OPTIONS", "--target", str(self.root)
        ).strip()
        fields = filesystem.split()
        if len(fields) < 3 or fields[1] != "xfs":
            raise WorkspaceStorageError("Workspace storage must reside on an XFS filesystem.")
        if Path(fields[0]).resolve() != self.root.resolve():
            raise WorkspaceStorageError("Workspace storage root must be a dedicated mount point.")
        options = set(fields[2].split(","))
        if not options.intersection({"prjquota", "pquota", "project"}):
            raise WorkspaceStorageError(
                "XFS project quotas are not enabled on the workspace storage mount."
            )
        state = self._run(
            "xfs_quota", "-x", "-c", "state -p", str(self.root)
        )
        if not re.search(r"Enforcement:\s*ON", state, flags=re.IGNORECASE):
            raise WorkspaceStorageError(
                "XFS project quota enforcement is not active on the workspace mount."
            )

    def _workspace_path(self, workspace_id: str) -> tuple[str, Path]:
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise HTTPException(status_code=422, detail="Invalid workspace ID.")
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
        return digest, self.root / digest

    def _project_id(self, workspace_id: str) -> int:
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
        return (int(digest[:8], 16) & 0x7FFFFFFF) or 1

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.registry, timeout=30, isolation_level=None)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_projects (
                workspace_id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL UNIQUE,
                directory TEXT NOT NULL UNIQUE
            )"""
        )
        return connection

    def _register_project(self, workspace_id: str, directory: Path) -> int:
        project_id = self._project_id(workspace_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT project_id, directory FROM workspace_projects WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if existing:
                if existing[1] != str(directory):
                    raise WorkspaceStorageError("Workspace storage registry path mismatch.")
                connection.commit()
                return int(existing[0])
            collision = connection.execute(
                "SELECT workspace_id FROM workspace_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if collision:
                raise WorkspaceStorageError(
                    "XFS project ID collision; refusing to share a disk quota."
                )
            connection.execute(
                "INSERT INTO workspace_projects(workspace_id, project_id, directory) "
                "VALUES (?, ?, ?)",
                (workspace_id, project_id, str(directory)),
            )
            connection.commit()
            return project_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_workspace(self, workspace_id: str) -> Path:
        _, directory = self._workspace_path(workspace_id)
        self.validate_backend()
        if not self.registry.exists() and any(self.root.iterdir()):
            raise WorkspaceStorageError(
                "Workspace registry is missing while storage data exists; recover the registry before provisioning."
            )
        project_id = self._register_project(workspace_id, directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise WorkspaceStorageError("Workspace storage directory escaped its root.")
        self._run(
            "xfs_quota",
            "-x",
            "-c",
            f"project -s -p {directory} {project_id}",
            str(self.root),
        )
        self._run(
            "xfs_quota",
            "-x",
            "-c",
            f"limit -p bhard={self.quota_limit} ihard={self.inode_limit} {project_id}",
            str(self.root),
        )
        return directory

    def delete_workspace(self, workspace_id: str) -> None:
        _, directory = self._workspace_path(workspace_id)
        self.validate_backend()
        if not self.registry.exists():
            if directory.exists() or any(self.root.iterdir()):
                raise WorkspaceStorageError(
                    "Workspace registry is missing while storage data exists; recover it before deletion."
                )
            return
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT project_id FROM workspace_projects WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        finally:
            connection.close()
        if row:
            project_id = int(row[0])
            if directory.exists():
                self._run(
                    "xfs_quota",
                    "-x",
                    "-c",
                    f"project -C -p {directory} {project_id}",
                    str(self.root),
                )
            self._run(
                "xfs_quota",
                "-x",
                "-c",
                f"limit -p bhard=0 ihard=0 {project_id}",
                str(self.root),
            )
        if directory.is_symlink():
            raise WorkspaceStorageError("Workspace storage path unexpectedly became a symlink.")
        if directory.exists():
            shutil.rmtree(directory)
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM workspace_projects WHERE workspace_id = ?", (workspace_id,)
            )
        finally:
            connection.close()
