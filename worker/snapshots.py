from __future__ import annotations

import base64
import hashlib
import hmac
import os
import posixpath
import re
import shutil
import sqlite3
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from worker.storage import XFSWorkspaceStorage

MAGIC = b"DBXSNP1\0"
NONCE_SIZE = 12
TAG_SIZE = 16
CHUNK_SIZE = 1024 * 1024
SNAPSHOT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class SnapshotError(RuntimeError):
    """Raised when snapshot creation, validation, or restoration fails."""


class SnapshotConflict(SnapshotError):
    """Raised when the requested snapshot operation conflicts with policy."""


class SnapshotNotFound(SnapshotError):
    """Raised when a snapshot does not belong to the requested workspace."""


class _EncryptingWriter:
    def __init__(self, target, encryptor, max_bytes: int) -> None:
        self.target = target
        self.encryptor = encryptor
        self.max_bytes = max_bytes
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        if self.bytes_written + len(data) > self.max_bytes:
            raise SnapshotError("Snapshot exceeds the configured archive size limit.")
        encrypted = self.encryptor.update(data)
        self.target.write(encrypted)
        self.bytes_written += len(data)
        return len(data)

    def flush(self) -> None:
        self.target.flush()


class _DecryptingReader:
    def __init__(self, source, decryptor, ciphertext_size: int, tag: bytes) -> None:
        self.source = source
        self.decryptor = decryptor
        self.remaining = ciphertext_size
        self.tag = tag
        self.buffer = bytearray()
        self.finished = False

    def _fill(self, requested: int) -> None:
        while len(self.buffer) < requested and not self.finished:
            if self.remaining:
                chunk = self.source.read(min(CHUNK_SIZE, self.remaining))
                if not chunk:
                    raise SnapshotError("Snapshot ciphertext is truncated.")
                self.remaining -= len(chunk)
                self.buffer.extend(self.decryptor.update(chunk))
            else:
                try:
                    self.buffer.extend(self.decryptor.finalize_with_tag(self.tag))
                except InvalidTag as exc:
                    raise SnapshotError(
                        "Snapshot authentication failed; the file or encryption key is invalid."
                    ) from exc
                self.finished = True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while not self.finished:
                self._fill(len(self.buffer) + CHUNK_SIZE)
            size = len(self.buffer)
        elif size:
            self._fill(size)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def verify(self) -> None:
        while not self.finished:
            self._fill(len(self.buffer) + CHUNK_SIZE)
        if self.buffer and any(self.buffer):
            raise SnapshotError("Snapshot contains unexpected data after the archive.")


class SnapshotStore:
    """Encrypted snapshots stored on a separately quota-limited XFS mount."""

    def __init__(
        self,
        storage: XFSWorkspaceStorage,
        workspace_storage: XFSWorkspaceStorage,
        encryption_key: str,
        *,
        max_snapshots: int = 10,
        max_snapshot_bytes: int = 10 * 1024 * 1024 * 1024,
        max_entries: int = 200_000,
    ) -> None:
        key = None
        key_error = None
        if encryption_key:
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", encryption_key):
                key_error = (
                    "Snapshot encryption key must be a 32-byte URL-safe base64 value."
                )
            else:
                try:
                    key = base64.urlsafe_b64decode(encryption_key + "=")
                except (ValueError, TypeError):
                    key_error = "Snapshot encryption key must be URL-safe base64."
                if key is not None and len(key) != 32:
                    key = None
                    key_error = "Snapshot encryption key must decode to exactly 32 bytes."
        if max_snapshots < 1 or max_snapshot_bytes < 1 or max_entries < 1:
            raise SnapshotError("Snapshot limits must be positive.")
        self.storage = storage
        self.workspace_storage = workspace_storage
        self.key = key
        self.key_error = key_error
        self.max_snapshots = max_snapshots
        self.max_snapshot_bytes = max_snapshot_bytes
        self.max_entries = max_entries
        self.registry = storage.root / ".devbox-snapshots.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.registry, timeout=30)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                UNIQUE(workspace_id, snapshot_id)
            )"""
        )
        return connection

    def _workspace_directory(self, workspace_id: str) -> Path:
        directory = self.storage.ensure_workspace(workspace_id)
        try:
            self.storage.owner_setter(directory, 0, 0)
            directory.chmod(0o700)
        except OSError as exc:
            raise SnapshotError("Unable to secure the snapshot directory.") from exc

        if not self.registry.exists() and any(
            entry.name.endswith(".dbxsnap") or entry.name.endswith(".dbxsnap.tmp")
            for entry in directory.iterdir()
        ):
            raise SnapshotError(
                "Snapshot metadata registry is missing; recover it before modifying snapshots."
            )
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT filename FROM snapshots WHERE workspace_id = ?", (workspace_id,)
            ).fetchall()
        finally:
            connection.close()
        known_files = {row[0] for row in rows}
        for entry in directory.iterdir():
            if entry.is_symlink():
                raise SnapshotError("Snapshot directory contains an unexpected symbolic link.")
            if re.fullmatch(r"\.[0-9a-f]{32}\.dbxsnap\.tmp", entry.name):
                try:
                    entry.unlink(missing_ok=True)
                except OSError as exc:
                    raise SnapshotError("Unable to clean an interrupted snapshot file.") from exc
            elif re.fullmatch(r"[0-9a-f]{32}\.dbxsnap", entry.name):
                if entry.name not in known_files:
                    try:
                        entry.unlink(missing_ok=True)
                    except OSError as exc:
                        raise SnapshotError(
                            "Unable to clean an orphaned encrypted snapshot."
                        ) from exc
                elif not entry.is_file():
                    raise SnapshotError("Registered snapshot path is not a regular file.")
            else:
                raise SnapshotError("Snapshot directory contains an unexpected entry.")
        missing_files = []
        for filename in known_files:
            if not re.fullmatch(r"[0-9a-f]{32}\.dbxsnap", filename):
                raise SnapshotError("Snapshot registry contains an unsafe filename.")
            if not (directory / filename).is_file():
                missing_files.append(filename)
        if missing_files:
            connection = self._connect()
            try:
                connection.executemany(
                    "DELETE FROM snapshots WHERE workspace_id = ? AND filename = ?",
                    [(workspace_id, filename) for filename in missing_files],
                )
                connection.commit()
            finally:
                connection.close()
        return directory

    def _require_key(self) -> bytes:
        if self.key is None:
            raise SnapshotError(
                self.key_error or "Snapshot encryption key is not configured."
            )
        return self.key

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        directory_flag = getattr(os, "O_DIRECTORY", None)

        # Windows does not support opening directories for fsync through os.open.
        # File contents are flushed separately before the atomic replacement.
        if directory_flag is None:
            return

        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _aad(self, workspace_id: str, snapshot_id: str) -> bytes:
        return f"devbox:{workspace_id}:{snapshot_id}".encode()

    @staticmethod
    def _safe_member(member: tarfile.TarInfo) -> str:
        name = member.name
        if "\x00" in name or "\\" in name or name.startswith("/"):
            raise SnapshotError("Snapshot contains an unsafe archive path.")
        if ".." in name.split("/"):
            raise SnapshotError("Snapshot path contains a parent-directory component.")
        normalized = posixpath.normpath(name)
        if normalized in ("", "."):
            return "."
        if normalized == ".." or normalized.startswith("../"):
            raise SnapshotError("Snapshot path escapes the workspace.")
        if not (member.isdir() or member.isfile() or member.issym()):
            raise SnapshotError("Snapshot contains an unsupported filesystem object.")
        if member.issym():
            target = member.linkname
            if not target or target.startswith("/") or "\\" in target or "\x00" in target:
                raise SnapshotError("Snapshot contains an unsafe symbolic link.")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(normalized), target))
            if resolved == ".." or resolved.startswith("../"):
                raise SnapshotError("Snapshot symbolic link escapes the workspace.")
        return normalized

    def _archive_filter(self, member: tarfile.TarInfo) -> tarfile.TarInfo:
        self._safe_member(member)
        return member

    def create(self, workspace_id: str, workspace_path: Path) -> dict:
        key = self._require_key()
        self.recover_restore(workspace_id)
        _, expected_workspace_path = self.workspace_storage._workspace_path(workspace_id)
        if (
            workspace_path != expected_workspace_path
            or workspace_path.is_symlink()
            or not workspace_path.is_dir()
            or workspace_path.resolve().parent != self.workspace_storage.root.resolve()
        ):
            raise SnapshotError("Snapshot source does not match the assigned workspace directory.")
        snapshot_directory = self._workspace_directory(workspace_id)
        connection = self._connect()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()[0]
            if count >= self.max_snapshots:
                raise SnapshotConflict(
                    f"Snapshot retention limit reached ({self.max_snapshots}). "
                    "Delete an older snapshot before creating another."
                )
        finally:
            connection.close()

        snapshot_id = uuid.uuid4().hex
        filename = f"{snapshot_id}.dbxsnap"
        final_path = snapshot_directory / filename
        temporary_path = snapshot_directory / f".{filename}.tmp"
        nonce = os.urandom(NONCE_SIZE)
        encryptor = Cipher(
            algorithms.AES(key), modes.GCM(nonce)
        ).encryptor()
        encryptor.authenticate_additional_data(self._aad(workspace_id, snapshot_id))
        try:
            with temporary_path.open("xb") as output:
                output.write(MAGIC + nonce)
                writer = _EncryptingWriter(output, encryptor, self.max_snapshot_bytes)
                entry_count = 0

                def archive_filter(member: tarfile.TarInfo) -> tarfile.TarInfo:
                    nonlocal entry_count
                    filtered_member = self._archive_filter(member)
                    entry_count += 1
                    if entry_count > self.max_entries:
                        raise SnapshotError(
                            "Snapshot exceeds the configured entry limit."
                        )
                    return filtered_member

                with tarfile.open(fileobj=writer, mode="w|") as archive:
                    archive.add(
                        str(workspace_path),
                        arcname=".",
                        recursive=True,
                        filter=archive_filter,
                    )
                writer.flush()
                output.write(encryptor.finalize())
                output.write(encryptor.tag)
                output.flush()
                os.fsync(output.fileno())
            encrypted_hash = hashlib.sha256()
            with temporary_path.open("rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    encrypted_hash.update(chunk)
            os.replace(temporary_path, final_path)
            self._fsync_directory(snapshot_directory)
            created_at = datetime.now(UTC).isoformat()
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT INTO snapshots "
                    "(snapshot_id, workspace_id, filename, created_at, size_bytes, sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        workspace_id,
                        filename,
                        created_at,
                        final_path.stat().st_size,
                        encrypted_hash.hexdigest(),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            return {
                "snapshot_id": snapshot_id,
                "workspace_id": workspace_id,
                "created_at": created_at,
                "size_bytes": final_path.stat().st_size,
                "sha256": encrypted_hash.hexdigest(),
            }
        except Exception as exc:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            if isinstance(exc, SnapshotError):
                raise
            raise SnapshotError("Snapshot creation failed.") from exc

    def list(self, workspace_id: str) -> list[dict]:
        self._workspace_directory(workspace_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT snapshot_id, created_at, size_bytes, sha256 "
                "FROM snapshots WHERE workspace_id = ? ORDER BY created_at DESC",
                (workspace_id,),
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "snapshot_id": row[0],
                "workspace_id": workspace_id,
                "created_at": row[1],
                "size_bytes": row[2],
                "sha256": row[3],
            }
            for row in rows
        ]

    def _snapshot_record(self, workspace_id: str, snapshot_id: str) -> tuple[Path, str]:
        if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
            raise SnapshotNotFound("Snapshot not found.")
        directory = self._workspace_directory(workspace_id)
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT filename, sha256 FROM snapshots "
                "WHERE workspace_id = ? AND snapshot_id = ?",
                (workspace_id, snapshot_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SnapshotNotFound("Snapshot not found.")
        if not re.fullmatch(r"[0-9a-f]{32}\.dbxsnap", row[0]):
            raise SnapshotError("Snapshot registry contains an unsafe filename.")
        path = directory / row[0]
        if path.parent != directory or path.is_symlink() or not path.is_file():
            raise SnapshotError("Snapshot storage entry is missing or unsafe.")
        return path, row[1]

    def _decrypting_reader(self, source, workspace_id: str, snapshot_id: str):
        header = source.read(len(MAGIC) + NONCE_SIZE)
        if len(header) != len(MAGIC) + NONCE_SIZE or not header.startswith(MAGIC):
            raise SnapshotError("Snapshot header is invalid.")
        nonce = header[len(MAGIC):]
        file_size = os.fstat(source.fileno()).st_size
        ciphertext_size = file_size - len(header) - TAG_SIZE
        if ciphertext_size < 0:
            raise SnapshotError("Snapshot file is truncated.")
        source.seek(file_size - TAG_SIZE)
        tag = source.read(TAG_SIZE)
        source.seek(len(header))
        decryptor = Cipher(
            algorithms.AES(self._require_key()), modes.GCM(nonce)
        ).decryptor()
        decryptor.authenticate_additional_data(self._aad(workspace_id, snapshot_id))
        return _DecryptingReader(source, decryptor, ciphertext_size, tag)

    def _extract(self, encrypted_path: Path, workspace_id: str, snapshot_id: str, staging: Path):
        entries = set()
        symlinks = []
        total_size = 0
        with encrypted_path.open("rb") as source:
            reader = self._decrypting_reader(source, workspace_id, snapshot_id)
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    relative = self._safe_member(member)
                    if relative in entries:
                        raise SnapshotError("Snapshot contains duplicate archive paths.")
                    entries.add(relative)
                    if len(entries) > self.max_entries:
                        raise SnapshotError("Snapshot contains too many filesystem entries.")
                    if member.size < 0:
                        raise SnapshotError("Snapshot contains an invalid file size.")
                    total_size += member.size
                    if total_size > self.max_snapshot_bytes:
                        raise SnapshotError("Snapshot expands beyond the configured size limit.")
                    if relative == ".":
                        if not member.isdir():
                            raise SnapshotError("Snapshot root entry must be a directory.")
                        continue
                    destination = staging.joinpath(*PurePosixPath(relative).parts)
                    if staging not in destination.parents:
                        raise SnapshotError("Snapshot path escapes the restore directory.")
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        destination.chmod(0o755)
                    elif member.issym():
                        symlinks.append((destination, member.linkname))
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        input_file = archive.extractfile(member)
                        if input_file is None:
                            raise SnapshotError("Snapshot file contents are unavailable.")
                        with input_file, destination.open("xb") as output_file:
                            shutil.copyfileobj(input_file, output_file, CHUNK_SIZE)
                        destination.chmod(0o755 if member.mode & 0o111 else 0o644)
            reader.verify()

        for destination, target in symlinks:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise SnapshotError("Snapshot symbolic link collides with another entry.")
            os.symlink(target, destination)

        for root, directories, files in os.walk(staging, topdown=True, followlinks=False):
            for name in directories + files:
                path = Path(root) / name
                if not path.is_symlink():
                    self.workspace_storage.owner_setter(path, 10001, 10001)
            self.workspace_storage.owner_setter(Path(root), 10001, 10001)

    def recover_restore(self, workspace_id: str) -> None:
        self.workspace_storage.validate_backend()
        _, workspace_directory = self.workspace_storage._workspace_path(workspace_id)
        stage = workspace_directory.parent / f".{workspace_directory.name}.restore-stage"
        backup = workspace_directory.parent / f".{workspace_directory.name}.restore-backup"
        if workspace_directory.is_symlink() or (
            workspace_directory.exists() and not workspace_directory.is_dir()
        ):
            raise SnapshotError("Workspace restore target is not a safe directory.")
        for path in (stage, backup):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise SnapshotError("Restore recovery path is not a safe directory.")
        try:
            if backup.exists():
                if workspace_directory.exists():
                    self.workspace_storage.mark_workspace_initialized(
                        workspace_id, workspace_directory
                    )
                    shutil.rmtree(backup)
                else:
                    os.replace(backup, workspace_directory)
                    self.workspace_storage.mark_workspace_initialized(
                        workspace_id, workspace_directory
                    )
            if stage.exists():
                shutil.rmtree(stage)
        except OSError as exc:
            raise SnapshotError("Unable to recover interrupted workspace restore.") from exc

    def restore(self, workspace_id: str, snapshot_id: str, workspace_path: Path) -> dict:
        self._require_key()
        _, expected_workspace_path = self.workspace_storage._workspace_path(workspace_id)
        if workspace_path != expected_workspace_path:
            raise SnapshotError("Restore target does not match the assigned workspace directory.")
        encrypted_path, expected_hash = self._snapshot_record(workspace_id, snapshot_id)
        digest = hashlib.sha256()
        with encrypted_path.open("rb") as source:
            while chunk := source.read(CHUNK_SIZE):
                digest.update(chunk)
        if not hmac_compare(digest.hexdigest(), expected_hash):
            raise SnapshotError("Snapshot checksum verification failed.")

        self.recover_restore(workspace_id)
        _, workspace_directory = self.workspace_storage._workspace_path(workspace_id)
        project_id = self.workspace_storage._project_id(workspace_id)
        stage = workspace_directory.parent / f".{workspace_directory.name}.restore-stage"
        backup = workspace_directory.parent / f".{workspace_directory.name}.restore-backup"
        try:
            self.workspace_storage.set_project_quota(workspace_id, multiplier=3)
            self.workspace_storage.prepare_project_directory(
                workspace_id, stage, project_id, quota_multiplier=3
            )
            self._extract(encrypted_path, workspace_id, snapshot_id, stage)
            if workspace_directory.exists():
                os.replace(workspace_directory, backup)
                self._fsync_directory(workspace_directory.parent)
            try:
                os.replace(stage, workspace_directory)
                self._fsync_directory(workspace_directory.parent)
                self.workspace_storage.mark_workspace_initialized(
                    workspace_id, workspace_directory
                )
            except Exception:
                if backup.exists() and not workspace_directory.exists():
                    os.replace(backup, workspace_directory)
                raise
            if backup.exists():
                shutil.rmtree(backup)
                self._fsync_directory(workspace_directory.parent)
            self.workspace_storage.set_project_quota(workspace_id, multiplier=1)
            return {
                "snapshot_id": snapshot_id,
                "workspace_id": workspace_id,
                "state": "restored",
            }
        except Exception as exc:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if backup.exists() and not workspace_directory.exists():
                os.replace(backup, workspace_directory)
                self._fsync_directory(workspace_directory.parent)
            if not backup.exists():
                self.workspace_storage.set_project_quota(workspace_id, multiplier=1)
            if isinstance(exc, SnapshotError):
                raise
            raise SnapshotError(
                "Snapshot restore failed; the previous workspace was retained."
            ) from exc

    def delete(self, workspace_id: str, snapshot_id: str) -> None:
        path, _ = self._snapshot_record(workspace_id, snapshot_id)
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM snapshots WHERE workspace_id = ? AND snapshot_id = ?",
                (workspace_id, snapshot_id),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise SnapshotError("Unable to remove the encrypted snapshot file.") from exc

    def delete_workspace(self, workspace_id: str) -> None:
        self._workspace_directory(workspace_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT filename FROM snapshots WHERE workspace_id = ?", (workspace_id,)
            ).fetchall()
        finally:
            connection.close()
        directory = self.storage._workspace_path(workspace_id)[1]
        for row in rows:
            if not re.fullmatch(r"[0-9a-f]{32}\.dbxsnap", row[0]):
                raise SnapshotError("Snapshot registry contains an unsafe filename.")
            path = directory / row[0]
            if path.parent != directory or path.is_symlink():
                raise SnapshotError("Snapshot registry path is unsafe.")
        connection = self._connect()
        try:
            connection.execute("DELETE FROM snapshots WHERE workspace_id = ?", (workspace_id,))
            connection.commit()
        finally:
            connection.close()
        try:
            for row in rows:
                (directory / row[0]).unlink(missing_ok=True)
        except OSError as exc:
            raise SnapshotError("Unable to remove all workspace snapshots.") from exc
        self.storage.delete_workspace(workspace_id)


def hmac_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
