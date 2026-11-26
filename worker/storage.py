from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

WORKSPACE_ID_PATTERN = re.compile(r"^dbx_[A-Za-z0-9_-]{20,40}$")
QUOTA_PATTERN = re.compile(r"^[1-9][0-9]*(?:[kKmMgGtT])?$")


class WorkspaceStorageError(RuntimeError):
    """Raised when the configured storage backend cannot enforce its policy."""


def _set_owner(path: Path, uid: int, gid: int) -> None:
    """Set ownership where the host platform supports POSIX ownership changes."""
    chown = getattr(os, "chown", None)
    if chown is None:
        raise OSError("Changing file ownership requires a POSIX platform with os.chown.")
    chown(path, uid, gid)


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
        owner_setter=_set_owner,
        registry_path: str | Path | None = None,
    ) -> None:
        # Storage is provisioned on Linux, but importing/configuring the worker on
        # Windows must still accept its POSIX deployment paths. Also accept native
        # absolute paths so unit tests can use temporary directories on either OS.
        if not (Path(root).is_absolute() or PurePosixPath(root).is_absolute()):
            raise WorkspaceStorageError("Workspace storage root must be an absolute path.")
        if not QUOTA_PATTERN.fullmatch(quota_limit):
            raise WorkspaceStorageError("Workspace disk quota must be a positive size such as 10g.")
        self.root = Path(root)
        if not all(char.isalnum() or char in "_./:\\\\-" for char in str(self.root)):
            raise WorkspaceStorageError(
                "Workspace storage root contains unsafe command characters."
            )
        if inode_limit < 1:
            raise WorkspaceStorageError("Workspace inode limit must be a positive integer.")
        self.quota_limit = quota_limit.lower()
        self.inode_limit = inode_limit
        self.runner = runner
        self.owner_setter = owner_setter
        self.legacy_registry = self.root / ".devbox-storage.sqlite3"
        self.registry = Path(registry_path) if registry_path else self.legacy_registry
        if not self.registry.is_absolute():
            raise WorkspaceStorageError("Workspace registry path must be absolute.")

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
        accounting_enabled = re.search(r"Accounting:\s*ON", state, flags=re.IGNORECASE)
        enforcement_enabled = re.search(r"Enforcement:\s*ON", state, flags=re.IGNORECASE)
        if not accounting_enabled or not enforcement_enabled:
            raise WorkspaceStorageError(
                "XFS project quota accounting and enforcement must both be active."
            )

    def _workspace_path(self, workspace_id: str) -> tuple[str, Path]:
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise HTTPException(status_code=422, detail="Invalid workspace ID.")
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
        return digest, self.root / digest

    def _project_id(self, workspace_id: str) -> int:
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
        return (int(digest[:8], 16) & 0x7FFFFFFF) or 1

    def _migrate_legacy_registry(self) -> None:
        if self.registry.is_symlink():
            raise WorkspaceStorageError("Broker-owned workspace registry is unsafe.")
        if self.registry == self.legacy_registry or self.registry.exists():
            return
        legacy = self.legacy_registry
        if not legacy.exists():
            return
        if legacy.is_symlink() or not legacy.is_file():
            raise WorkspaceStorageError("Legacy workspace registry is unsafe.")
        try:
            self.registry.parent.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(legacy)
            destination = sqlite3.connect(self.registry)
            try:
                source.backup(destination)
                destination.commit()
            finally:
                destination.close()
                source.close()
            self.registry.chmod(0o600)
            legacy.unlink()
        except (OSError, sqlite3.Error) as exc:
            raise WorkspaceStorageError(
                "Unable to migrate the workspace project registry to broker-owned storage."
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        if self.registry.is_symlink():
            raise WorkspaceStorageError("Workspace project registry is an unexpected symlink.")
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.registry, timeout=30, isolation_level=None)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS workspace_projects (
                workspace_id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL UNIQUE,
                directory TEXT NOT NULL UNIQUE,
                initialized INTEGER NOT NULL DEFAULT 0,
                device INTEGER,
                inode INTEGER
            )"""
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workspace_projects)")
        }
        if "initialized" not in columns:
            connection.execute(
                "ALTER TABLE workspace_projects "
                "ADD COLUMN initialized INTEGER NOT NULL DEFAULT 0"
            )
        if "device" not in columns:
            connection.execute("ALTER TABLE workspace_projects ADD COLUMN device INTEGER")
        if "inode" not in columns:
            connection.execute("ALTER TABLE workspace_projects ADD COLUMN inode INTEGER")
        return connection

    def _register_project(
        self, workspace_id: str, directory: Path
    ) -> tuple[int, bool, int | None, int | None]:
        project_id = self._project_id(workspace_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT project_id, directory, initialized, device, inode "
                "FROM workspace_projects WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if existing:
                if existing[1] != str(directory):
                    raise WorkspaceStorageError("Workspace storage registry path mismatch.")
                connection.commit()
                return (
                    int(existing[0]),
                    bool(existing[2]),
                    self._decode_identity(existing[3]),
                    self._decode_identity(existing[4]),
                )
            collision = connection.execute(
                "SELECT workspace_id FROM workspace_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            if collision:
                raise WorkspaceStorageError(
                    "XFS project ID collision; refusing to share a disk quota."
                )
            connection.execute(
                "INSERT INTO workspace_projects(workspace_id, project_id, directory, initialized) "
                "VALUES (?, ?, ?, 0)",
                (workspace_id, project_id, str(directory)),
            )
            connection.commit()
            return project_id, False, None, None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _encode_identity(value: int) -> str:
        # Prefix hexadecimal values so SQLite INTEGER affinity cannot coerce
        # large filesystem identifiers to imprecise floating-point REAL values.
        return f"x{value:x}"

    @staticmethod
    def _decode_identity(value: int | str | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("x"):
            return int(value[1:], 16)
        # Compatibility with registry rows written by older versions.
        return int(value)

    def _set_initialized(self, workspace_id: str, directory: Path) -> None:
        stat = directory.stat(follow_symlinks=False)
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE workspace_projects "
                "SET initialized = 1, device = ?, inode = ? WHERE workspace_id = ?",
                (
                    self._encode_identity(stat.st_dev),
                    self._encode_identity(stat.st_ino),
                    workspace_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkspaceStorageError("Workspace project registry entry is missing.")
        finally:
            connection.close()

    def mark_workspace_initialized(self, workspace_id: str, directory: Path) -> None:
        _, expected_directory = self._workspace_path(workspace_id)
        if (
            directory != expected_directory
            or directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve().parent != self.root.resolve()
        ):
            raise WorkspaceStorageError("Workspace directory cannot be registered safely.")
        self._set_initialized(workspace_id, directory)

    def ensure_workspace(self, workspace_id: str) -> Path:
        _, directory = self._workspace_path(workspace_id)
        self.validate_backend()
        self._migrate_legacy_registry()
        if not self.registry.exists():
            unexpected_entries = [
                entry
                for entry in self.root.iterdir()
                if entry.name != "lost+found" or entry.is_symlink() or not entry.is_dir()
            ]
            if unexpected_entries:
                raise WorkspaceStorageError(
                    "Workspace registry is missing while storage data exists; "
                    "recover the registry before provisioning."
                )
        project_id, initialized, registered_device, registered_inode = self._register_project(
            workspace_id, directory
        )
        if directory.is_symlink():
            raise WorkspaceStorageError("Workspace storage directory is an unexpected symlink.")
        directory_existed = directory.exists()
        if initialized and not directory_existed:
            raise WorkspaceStorageError(
                "Registered workspace directory is missing; restore it from a snapshot."
            )
        if directory_existed and not directory.is_dir():
            raise WorkspaceStorageError("Workspace storage path is not a directory.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise WorkspaceStorageError("Workspace storage directory escaped its root.")
        directory_stat = directory.stat(follow_symlinks=False)
        if initialized and registered_device is not None and registered_inode is not None:
            if (directory_stat.st_dev, directory_stat.st_ino) != (
                registered_device,
                registered_inode,
            ):
                raise WorkspaceStorageError(
                    "Workspace directory identity changed; refusing to attach replaced data."
                )
        try:
            self.owner_setter(directory, 10001, 10001)
            directory.chmod(0o700)
        except OSError as exc:
            raise WorkspaceStorageError(
                "Unable to assign workspace directory ownership to the unprivileged runtime user."
            ) from exc
        if not initialized:
            self._run(
                "xfs_quota",
                "-x",
                "-c",
                f"project -s -p {directory} {project_id}",
                str(self.root),
            )
            self._set_initialized(workspace_id, directory)
        elif registered_device is None or registered_inode is None:
            self._set_initialized(workspace_id, directory)
        self._run(
            "xfs_quota",
            "-x",
            "-c",
            f"limit -p bhard={self.quota_limit} ihard={self.inode_limit} {project_id}",
            str(self.root),
        )
        return directory

    def set_project_quota(self, workspace_id: str, multiplier: int = 1) -> None:
        self._workspace_path(workspace_id)
        if multiplier < 1 or multiplier > 10:
            raise WorkspaceStorageError("Workspace quota multiplier must be between 1 and 10.")
        match = re.fullmatch(r"([1-9][0-9]*)([kmgt]?)", self.quota_limit)
        if match is None:
            raise WorkspaceStorageError("Workspace disk quota configuration is invalid.")
        quota = f"{int(match.group(1)) * multiplier}{match.group(2)}"
        self._run(
            "xfs_quota",
            "-x",
            "-c",
            f"limit -p bhard={quota} ihard={self.inode_limit * multiplier} "
            f"{self._project_id(workspace_id)}",
            str(self.root),
        )

    def prepare_project_directory(
        self,
        workspace_id: str,
        directory: Path,
        project_id: int,
        quota_multiplier: int = 1,
    ) -> None:
        digest, workspace_directory = self._workspace_path(workspace_id)
        expected_staging = self.root / f".{workspace_directory.name}.restore-stage"
        if directory != expected_staging or directory.name != f".{digest}.restore-stage":
            raise WorkspaceStorageError("Staging directory name does not match the workspace.")
        if directory.parent.resolve() != self.root.resolve():
            raise WorkspaceStorageError(
                "Staging directory must be a direct child of the storage root."
            )
        if directory.is_symlink() or directory.exists():
            raise WorkspaceStorageError("Staging directory already exists or is unsafe.")
        if project_id != self._project_id(workspace_id):
            raise WorkspaceStorageError("Staging project ID does not match the workspace.")
        self._migrate_legacy_registry()
        if not self.registry.exists():
            unexpected_entries = [
                entry
                for entry in self.root.iterdir()
                if entry.name != "lost+found" or entry.is_symlink() or not entry.is_dir()
            ]
            if unexpected_entries:
                raise WorkspaceStorageError(
                    "Workspace registry is missing while storage data exists; "
                    "recover the registry before restoring."
                )
        registered_project_id, _, _, _ = self._register_project(
            workspace_id, workspace_directory
        )
        if registered_project_id != project_id:
            raise WorkspaceStorageError("Restore project ID does not match the registry.")
        directory.mkdir(mode=0o700)
        try:
            self.owner_setter(directory, 10001, 10001)
            directory.chmod(0o700)
        except OSError as exc:
            raise WorkspaceStorageError(
                "Unable to secure the restore staging directory."
            ) from exc
        self._run(
            "xfs_quota",
            "-x",
            "-c",
            f"project -s -p {directory} {project_id}",
            str(self.root),
        )
        self.set_project_quota(workspace_id, quota_multiplier)

    def delete_workspace(self, workspace_id: str) -> None:
        _, directory = self._workspace_path(workspace_id)
        self.validate_backend()
        self._migrate_legacy_registry()
        if not self.registry.exists():
            if directory.exists() or directory.is_symlink():
                raise WorkspaceStorageError(
                    "Workspace registry is missing for this workspace; recover it before deletion."
                )
            return
        if directory.is_symlink() or (
            directory.exists() and directory.resolve().parent != self.root.resolve()
        ):
            raise WorkspaceStorageError("Workspace storage path unexpectedly escaped its root.")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT project_id, directory, device, inode "
                "FROM workspace_projects WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        finally:
            connection.close()
        if row:
            project_id = int(row[0])
            if row[1] != str(directory):
                raise WorkspaceStorageError("Workspace storage registry path mismatch.")
            if directory.exists():
                directory_stat = directory.stat(follow_symlinks=False)
                if row[2] is not None and row[3] is not None:
                    if (directory_stat.st_dev, directory_stat.st_ino) != (
                        self._decode_identity(row[2]),
                        self._decode_identity(row[3]),
                    ):
                        raise WorkspaceStorageError(
                            "Workspace directory identity changed; refusing deletion."
                        )
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
        elif directory.exists():
            raise WorkspaceStorageError(
                "Workspace directory exists without a project registry entry; refusing deletion."
            )
        if directory.exists():
            shutil.rmtree(directory)
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM workspace_projects WHERE workspace_id = ?", (workspace_id,)
            )
        finally:
            connection.close()
