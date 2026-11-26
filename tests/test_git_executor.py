import pytest
from fastapi import HTTPException

from worker import main as executor
from worker.broker_client import BrokerRejected


class FakeGitBroker:
    def __init__(self, output="git operation complete", error=None, removed=0):
        self.output = output
        self.error = error
        self.removed = removed
        self.calls = []
        self.reconcile_calls = 0

    def run_git_helper(self, workspace_id, arguments, *, network_enabled=False):
        self.calls.append((workspace_id, arguments, network_enabled))
        if self.error:
            raise self.error
        return self.output

    def reconcile_git_helpers(self):
        self.reconcile_calls += 1
        return self.removed

class FakeGitStorage:
    def __init__(self, path):
        self.path = path

    def ensure_workspace(self, workspace_id):
        return self.path


def test_git_repository_url_allows_only_supported_https_hosts():
    assert executor.validate_git_repository_url(
        "https://github.com/example/project.git"
    ) == "https://github.com/example/project"

    for url in (
        "http://github.com/example/project",
        "https://example.com/project/repo",
        "https://user:secret@github.com/example/project",
        "https://github.com/example/project/extra",
        "https://127.0.0.1/example/project",
    ):
        with pytest.raises(HTTPException) as error:
            executor.validate_git_repository_url(url)
        assert error.value.status_code == 422


@pytest.mark.parametrize(
    "branch",
    ["", "-option", "../escape", "feature//bad", "feature name", "feature.lock"],
)
def test_git_branch_validation_rejects_unsafe_names(branch):
    with pytest.raises(HTTPException):
        executor.validate_git_branch(branch)


def test_git_helper_uses_network_none_for_local_operations(monkeypatch, tmp_path):
    fake = FakeGitBroker()
    monkeypatch.setattr(executor, "broker_client", fake)
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))

    output = executor.run_git_command(
        "dbx_abcdefghijklmnopqrstuv", ["status", "--short", "--branch"]
    )

    assert output == "git operation complete"
    assert fake.calls == [
        ("dbx_abcdefghijklmnopqrstuv", ["status", "--short", "--branch"], False)
    ]


def test_git_helper_uses_internal_proxy_for_fetch(monkeypatch, tmp_path):
    fake = FakeGitBroker()
    monkeypatch.setattr(executor, "broker_client", fake)
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))

    executor.run_git_command(
        "dbx_abcdefghijklmnopqrstuv",
        ["fetch", "--prune", "origin"],
        network_enabled=True,
    )

    assert fake.calls == [
        ("dbx_abcdefghijklmnopqrstuv", ["fetch", "--prune", "origin"], True)
    ]


def test_git_helper_surfaces_broker_rejection(monkeypatch, tmp_path):
    fake = FakeGitBroker(error=BrokerRejected(422, "remote branch not found"))
    monkeypatch.setattr(executor, "broker_client", fake)
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))

    with pytest.raises(HTTPException) as error:
        executor.run_git_command(
            "dbx_abcdefghijklmnopqrstuv",
            [
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--branch",
                "main",
                "--",
                "https://github.com/a/b",
                "/workspace",
            ],
            network_enabled=True,
        )

    assert error.value.status_code == 422
    assert "remote branch not found" in error.value.detail
    assert len(fake.calls) == 1


def test_reconciler_delegates_git_helper_cleanup_to_broker(monkeypatch):
    fake = FakeGitBroker(removed=1)
    monkeypatch.setattr(executor, "broker_client", fake)

    removed = executor.reconcile_orphaned_git_helpers()

    assert removed == 1
    assert fake.reconcile_calls == 1

def test_clone_refuses_nonempty_workspace_and_uses_proxy_for_clone(monkeypatch, tmp_path):
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    calls = []

    class Storage(FakeGitStorage):
        pass

    monkeypatch.setattr(executor, "workspace_storage", Storage(tmp_path))
    monkeypatch.setattr(
        executor,
        "run_git_command",
        lambda requested_id, arguments, network_enabled=False: (
            calls.append((requested_id, arguments, network_enabled)) or "ok"
        ),
    )

    result = executor.clone_workspace_repository(
        workspace_id,
        executor.GitCloneRequest(
            repository_url="https://github.com/example/project",
            branch="main",
        ),
    )

    assert result["operation"] == "clone"
    assert result["branch"] == "main"
    assert calls[-1][2] is True
    assert calls[-1][1][-2:] == ["https://github.com/example/project", "/workspace"]

    (tmp_path / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        executor.clone_workspace_repository(
            workspace_id,
            executor.GitCloneRequest(
                repository_url="https://github.com/example/project",
                branch="main",
            ),
        )
    assert error.value.status_code == 409


def test_git_commit_only_stages_explicit_paths(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))
    monkeypatch.setattr(
        executor,
        "run_git_command",
        lambda workspace_id, arguments, network_enabled=False: (
            calls.append(arguments) or ("a" * 40 if arguments == ["rev-parse", "HEAD"] else "ok")
        ),
    )

    executor.git_commit(
        "dbx_abcdefghijklmnopqrstuv",
        executor.GitCommitRequest(message="Commit staged work"),
    )

    assert len(calls) == 2
    assert calls[0][0:2] == ["-c", "user.name=DevBox Agent"]
    assert "add" not in calls[0]
    assert calls[0][-2:] == ["-m", "Commit staged work"]
    assert calls[1] == ["rev-parse", "HEAD"]


def test_git_commit_rejects_path_traversal(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))

    with pytest.raises(HTTPException) as error:
        executor.git_commit(
            "dbx_abcdefghijklmnopqrstuv",
            executor.GitCommitRequest(message="Unsafe commit", paths=["../secrets.txt"]),
        )

    assert error.value.status_code == 422


def test_reconciler_removes_exited_git_helpers_but_keeps_running_helpers(monkeypatch):
    class Helper:
        def __init__(self, status):
            self.status = status
            self.removed = False

        def reload(self):
            return None

        def remove(self, force):
            self.removed = True

    class Containers:
        def __init__(self, items):
            self.items = items

        def list(self, **kwargs):
            assert kwargs == {
                "all": True,
                "filters": {"label": "devbox.git_helper=true"},
            }
            return self.items

    exited = Helper("exited")
    running = Helper("running")
    fake_docker = type(
        "Docker", (), {"containers": Containers([exited, running])}
    )()
    monkeypatch.setattr(executor, "docker_client", fake_docker)

    removed = executor.reconcile_orphaned_git_helpers()

    assert removed == 1
    assert exited.removed is True
    assert running.removed is False


def test_git_commit_stages_only_requested_relative_paths(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))
    monkeypatch.setattr(
        executor,
        "run_git_command",
        lambda workspace_id, arguments, network_enabled=False: (
            calls.append(arguments) or "ok"
        ),
    )

    executor.git_commit(
        "dbx_abcdefghijklmnopqrstuv",
        executor.GitCommitRequest(
            message="Commit one file",
            paths=["src/main.py"],
        ),
    )

    assert calls[0] == ["add", "--", "src/main.py"]
    assert calls[1][-2:] == ["-m", "Commit one file"]


def test_clone_is_idempotent_for_same_repository_and_branch(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))

    def fake_run(workspace_id, arguments, network_enabled=False):
        calls.append((arguments, network_enabled))
        if arguments == ["remote", "get-url", "origin"]:
            return "https://github.com/example/project"
        if arguments == ["branch", "--show-current"]:
            return "main"
        raise AssertionError(f"Unexpected Git command: {arguments}")

    monkeypatch.setattr(executor, "run_git_command", fake_run)

    result = executor.clone_workspace_repository(
        "dbx_abcdefghijklmnopqrstuv",
        executor.GitCloneRequest(
            repository_url="https://github.com/example/project",
            branch="main",
        ),
    )

    assert result["output"] == "Repository already cloned."
    assert len(calls) == 2
    assert all(network_enabled is False for _, network_enabled in calls)


def test_git_fetch_resets_origin_to_registered_repository(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))
    monkeypatch.setattr(
        executor,
        "run_git_command",
        lambda workspace_id, arguments, network_enabled=False: (
            calls.append((arguments, network_enabled)) or "ok"
        ),
    )

    executor.git_fetch(
        "dbx_abcdefghijklmnopqrstuv",
        executor.GitRepositoryRequest(
            repository_url="https://github.com/example/project"
        ),
    )

    assert calls == [
        (["remote", "set-url", "origin", "https://github.com/example/project"], False),
        (["fetch", "--prune", "origin"], True),
    ]
