import os
import stat

import pytest

import worker.instance_lock as instance_lock


pytestmark = pytest.mark.skipif(
    instance_lock.fcntl is None,
    reason="Advisory service locks are available only on Linux.",
)


def test_file_lock_rejects_a_second_instance_and_releases_on_close(tmp_path):
    path = tmp_path / "worker.lock"
    first = instance_lock.acquire_file_lock(path, "worker")
    try:
        with pytest.raises(RuntimeError, match="already holds"):
            instance_lock.acquire_file_lock(path, "worker")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text(encoding="ascii").strip() == str(os.getpid())
    finally:
        first.close()

    second = instance_lock.acquire_file_lock(path, "worker")
    second.close()


def test_file_lock_rejects_symlink_paths(tmp_path):
    target = tmp_path / "target"
    target.write_text("not a lock", encoding="utf-8")
    link = tmp_path / "worker.lock"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="lock path is unsafe"):
        instance_lock.acquire_file_lock(link, "worker")


def test_file_lock_requires_an_existing_state_directory(tmp_path):
    missing = tmp_path / "missing" / "worker.lock"

    with pytest.raises(RuntimeError, match="state directory is missing"):
        instance_lock.acquire_file_lock(missing, "worker")
