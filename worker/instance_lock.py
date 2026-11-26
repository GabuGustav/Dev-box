from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TextIO

try:
    import fcntl
except ImportError:  # pragma: no cover - the service containers run on Linux
    fcntl = None


def acquire_file_lock(path: Path, service_name: str) -> TextIO | None:
    """Acquire a non-blocking, process-wide lock on a persistent volume file.

    The returned handle must remain open for the lifetime of the service. Closing
    it releases the advisory lock. A missing parent directory is a deployment
    error: the broker/worker state volume must be initialized before startup.
    """
    if fcntl is None:
        return None
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise RuntimeError(f"The {service_name} state directory is missing or unsafe.")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"The {service_name} lock path is not a regular file.")
        if metadata.st_uid != os.geteuid():
            raise RuntimeError(f"The {service_name} lock file has an unexpected owner.")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Another {service_name} instance already holds the deployment lock."
            ) from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        return os.fdopen(descriptor, "r+", encoding="ascii")
    except Exception:
        os.close(descriptor)
        raise
