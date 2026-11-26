from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from worker.broker_client import BrokerClient, BrokerError
from worker.storage import QUOTA_PATTERN, WORKSPACE_ID_PATTERN, WorkspaceStorageError


class RemoteWorkspaceStorage:
    """Worker-side filesystem view backed by broker-owned XFS quota operations.

    File reads/writes still happen against the shared mount, but all XFS project
    registry and quota mutations are performed by the privileged broker.
    """

    def __init__(
        self,
        root: str,
        quota_limit: str,
        inode_limit: int,
        storage_kind: str,
        broker_client: BrokerClient,
    ) -> None:
        if not (Path(root).is_absolute() or PurePosixPath(root).is_absolute()):
            raise WorkspaceStorageError("Workspace storage root must be an absolute path.")
        if not QUOTA_PATTERN.fullmatch(quota_limit):
            raise WorkspaceStorageError("Workspace disk quota must be a positive size.")
        if inode_limit < 1:
            raise WorkspaceStorageError("Workspace inode limit must be a positive integer.")
        if storage_kind not in {"workspace", "snapshot"}:
            raise WorkspaceStorageError("Unknown broker storage kind.")
        self.root = Path(root)
        self.quota_limit = quota_limit.lower()
        self.inode_limit = inode_limit
        self.storage_kind = storage_kind
        self.broker_client = broker_client
        self.registry = self.root / ".devbox-storage.sqlite3"

    def _call(self, operation):
        try:
            return operation()
        except BrokerError as exc:
            raise WorkspaceStorageError(
                "Privileged broker storage operation failed."
            ) from exc

    def validate_backend(self) -> None:
        self._call(lambda: self.broker_client.validate_storage(self.storage_kind))

    def _workspace_path(self, workspace_id: str) -> tuple[str, Path]:
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise WorkspaceStorageError("Invalid workspace ID.")
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
        return digest, self.root / digest

    def _project_id(self, workspace_id: str) -> int:
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise WorkspaceStorageError("Invalid workspace ID.")
        digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
        return (int(digest[:8], 16) & 0x7FFFFFFF) or 1

    def ensure_workspace(self, workspace_id: str) -> Path:
        _, directory = self._workspace_path(workspace_id)
        self._call(
            lambda: self.broker_client.ensure_storage_workspace(
                self.storage_kind, workspace_id
            )
        )
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve().parent != self.root.resolve()
        ):
            raise WorkspaceStorageError(
                "Broker did not provision the expected workspace storage directory."
            )
        return directory

    def set_project_quota(self, workspace_id: str, multiplier: int = 1) -> None:
        self._workspace_path(workspace_id)
        if multiplier < 1 or multiplier > 10:
            raise WorkspaceStorageError("Workspace quota multiplier must be between 1 and 10.")
        self._call(
            lambda: self.broker_client.set_storage_quota(
                self.storage_kind, workspace_id, multiplier
            )
        )

    def prepare_project_directory(
        self,
        workspace_id: str,
        directory: Path,
        project_id: int,
        quota_multiplier: int = 1,
    ) -> None:
        digest, workspace_directory = self._workspace_path(workspace_id)
        expected_stage = self.root / f".{workspace_directory.name}.restore-stage"
        if (
            directory != expected_stage
            or directory.name != f".{digest}.restore-stage"
            or project_id != self._project_id(workspace_id)
        ):
            raise WorkspaceStorageError("Restore staging path or project ID is invalid.")
        if quota_multiplier < 1 or quota_multiplier > 10:
            raise WorkspaceStorageError("Workspace quota multiplier must be between 1 and 10.")
        self._call(
            lambda: self.broker_client.prepare_restore_directory(
                self.storage_kind, workspace_id, quota_multiplier
            )
        )
        if directory.is_symlink() or not directory.is_dir():
            raise WorkspaceStorageError(
                "Broker did not provision the expected restore staging directory."
            )

    def mark_workspace_initialized(self, workspace_id: str, directory: Path) -> None:
        _, expected_directory = self._workspace_path(workspace_id)
        if (
            directory != expected_directory
            or directory.is_symlink()
            or not directory.is_dir()
            or directory.resolve().parent != self.root.resolve()
        ):
            raise WorkspaceStorageError("Workspace directory cannot be registered safely.")
        self._call(
            lambda: self.broker_client.mark_storage_initialized(
                self.storage_kind, workspace_id
            )
        )

    def delete_workspace(self, workspace_id: str) -> None:
        self._workspace_path(workspace_id)
        self._call(
            lambda: self.broker_client.delete_storage_workspace(
                self.storage_kind, workspace_id
            )
        )
