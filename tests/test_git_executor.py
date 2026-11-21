import pytest
from fastapi import HTTPException

from worker import main as executor


class FakeGitContainer:
    def __init__(self, status_code=0, output=b"git operation complete"):
        self.status_code = status_code
        self.output = output
        self.started = False
        self.removed = False
        self.stopped = False

    def start(self):
        self.started = True

    def wait(self, timeout):
        assert timeout == executor.GIT_OPERATION_TIMEOUT_SECONDS
        return {"StatusCode": self.status_code}

    def logs(self, **kwargs):
        return self.output

    def stop(self, timeout):
        self.stopped = True

    def remove(self, force):
        self.removed = True


class FakeGitContainers:
    def __init__(self, container):
        self.container = container
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.container


class FakeGitDockerClient:
    def __init__(self, container):
        self.containers = FakeGitContainers(container)


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
    container = FakeGitContainer()
    fake = FakeGitDockerClient(container)
    monkeypatch.setattr(executor, "docker_client", fake)
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))

    output = executor.run_git_command(
        "dbx_abcdefghijklmnopqrstuv", ["status", "--short", "--branch"]
    )

    assert output == "git operation complete"
    assert fake.containers.kwargs["network_mode"] == "none"
    assert fake.containers.kwargs["user"] == "10001:10001"
    assert fake.containers.kwargs["cap_drop"] == ["ALL"]
    assert fake.containers.kwargs["read_only"] is True
    assert fake.containers.kwargs["labels"]["devbox.git_helper"] == "true"
    assert "devbox.managed" not in fake.containers.kwargs["labels"]
    assert fake.containers.kwargs["command"][:5] == [
        "timeout",
        "--signal=TERM",
        "--kill-after=5s",
        f"{executor.GIT_OPERATION_TIMEOUT_SECONDS}s",
        "git",
    ]
    assert fake.containers.kwargs["volumes"] == {
        str(tmp_path): {"bind": "/workspace", "mode": "rw"}
    }
    assert container.started is True
    assert container.removed is True


def test_git_helper_uses_only_internal_proxy_network_for_fetch(monkeypatch, tmp_path):
    container = FakeGitContainer()
    fake = FakeGitDockerClient(container)
    monkeypatch.setattr(executor, "docker_client", fake)
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))
    monkeypatch.setattr(executor, "GIT_EGRESS_NETWORK", "devbox_egress_internal")
    monkeypatch.setattr(executor, "GIT_PROXY_URL", "http://egress-proxy:3128")

    executor.run_git_command(
        "dbx_abcdefghijklmnopqrstuv", ["fetch", "--prune", "origin"], network_enabled=True
    )

    kwargs = fake.containers.kwargs
    assert kwargs["network_mode"] == "devbox_egress_internal"
    assert kwargs["environment"]["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert kwargs["environment"]["HTTP_PROXY"] == "http://egress-proxy:3128"


def test_git_helper_removes_container_after_failed_command(monkeypatch, tmp_path):
    container = FakeGitContainer(status_code=1, output=b"remote branch not found")
    fake = FakeGitDockerClient(container)
    monkeypatch.setattr(executor, "docker_client", fake)
    monkeypatch.setattr(executor, "workspace_storage", FakeGitStorage(tmp_path))

    with pytest.raises(HTTPException) as error:
        executor.run_git_command(
            "dbx_abcdefghijklmnopqrstuv", ["clone", "https://github.com/a/b", "/workspace"]
        )

    assert error.value.status_code == 422
    assert "remote branch not found" in error.value.detail
    assert container.removed is True


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
    assert calls[1][-2:] == ["-m", "Commit staged work"]


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
