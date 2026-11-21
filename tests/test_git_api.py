from app.routers import workspaces as workspace_routes
from tests.conftest import auth_headers


def create_workspace(client, branch="main"):
    response = client.post(
        "/api/v1/workspaces",
        json={
            "name": "Git test workspace",
            "repository_url": "https://github.com/example/project",
            "branch": branch,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_clone_uses_repository_and_branch_registered_on_workspace(client, monkeypatch):
    workspace = create_workspace(client, "feature/initial")
    calls = []

    def fake_clone(workspace_id, repository_url, branch):
        calls.append((workspace_id, repository_url, branch))
        return {"workspace_id": workspace_id, "operation": "clone", "branch": branch}

    monkeypatch.setattr(workspace_routes.executor_client, "git_clone", fake_clone)

    response = client.post(f"/api/v1/workspaces/{workspace['id']}/clone")

    assert response.status_code == 200
    assert calls == [
        (workspace["id"], "https://github.com/example/project", "feature/initial")
    ]


def test_git_operations_do_not_cross_workspace_ownership(client, monkeypatch):
    workspace = create_workspace(client)
    called = False

    def fake_status(workspace_id):
        nonlocal called
        called = True
        return {"workspace_id": workspace_id, "output": ""}

    monkeypatch.setattr(workspace_routes.executor_client, "git_status", fake_status)

    response = client.get(
        f"/api/v1/workspaces/{workspace['id']}/git/status",
        headers=auth_headers("test-key-two"),
    )

    assert response.status_code == 404
    assert called is False


def test_branch_creation_updates_workspace_branch_and_audits(client, monkeypatch):
    workspace = create_workspace(client)

    def fake_create_branch(workspace_id, branch):
        return {"workspace_id": workspace_id, "operation": "branch", "branch": branch}

    monkeypatch.setattr(
        workspace_routes.executor_client, "git_create_branch", fake_create_branch
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace['id']}/git/branches",
        json={"branch": "feature/new-work"},
    )

    assert response.status_code == 200
    assert response.json()["branch"] == "feature/new-work"
    updated = client.get(f"/api/v1/workspaces/{workspace['id']}")
    assert updated.json()["branch"] == "feature/new-work"
