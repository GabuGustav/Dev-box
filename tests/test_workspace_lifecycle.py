from app.routers import workspaces as workspace_routes
from tests.conftest import auth_headers


def create_workspace(client):
    response = client.post(
        "/api/v1/workspaces",
        json={
            "name": "Lifecycle test",
            "repository_url": "https://github.com/example/project",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def _result(self, action, workspace_id, state):
        self.calls.append((action, workspace_id))
        return {
            "workspace_id": workspace_id,
            "container_id": "container-test-id" if state != "deleted" else None,
            "storage_ref": "xfs-test",
            "state": state,
        }

    def start(self, workspace_id):
        return self._result("start", workspace_id, "running")

    def stop(self, workspace_id):
        return self._result("stop", workspace_id, "exited")

    def pause(self, workspace_id):
        return self._result("pause", workspace_id, "paused")

    def resume(self, workspace_id):
        return self._result("resume", workspace_id, "running")

    def delete(self, workspace_id):
        return self._result("delete", workspace_id, "deleted")


def test_workspace_lifecycle_transitions(client, monkeypatch):
    executor = FakeExecutor()
    monkeypatch.setattr(workspace_routes, "executor_client", executor)
    workspace_id = create_workspace(client)

    started = client.post(f"/api/v1/workspaces/{workspace_id}/start")
    assert started.status_code == 200
    assert started.json()["status"] == "running"
    assert started.json()["executor_id"] == "container-test-id"
    assert started.json()["storage_ref"] == "xfs-test"

    paused = client.post(f"/api/v1/workspaces/{workspace_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"

    resumed = client.post(f"/api/v1/workspaces/{workspace_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "running"

    stopped = client.post(f"/api/v1/workspaces/{workspace_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "stopped"

    deleted = client.delete(f"/api/v1/workspaces/{workspace_id}")
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert [call[0] for call in executor.calls] == [
        "start", "pause", "resume", "stop", "delete"
    ]


def test_other_principal_cannot_control_workspace(client, monkeypatch):
    executor = FakeExecutor()
    monkeypatch.setattr(workspace_routes, "executor_client", executor)
    workspace_id = create_workspace(client)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/start",
        headers=auth_headers("test-key-two"),
    )
    assert response.status_code == 404
    assert executor.calls == []


def test_deleted_workspace_cannot_restart(client, monkeypatch):
    executor = FakeExecutor()
    monkeypatch.setattr(workspace_routes, "executor_client", executor)
    workspace_id = create_workspace(client)

    assert client.delete(f"/api/v1/workspaces/{workspace_id}").status_code == 200
    response = client.post(f"/api/v1/workspaces/{workspace_id}/start")
    assert response.status_code == 409
    assert [call[0] for call in executor.calls] == ["delete"]


def test_other_principal_cannot_read_or_mutate_any_workspace_state(client, monkeypatch):
    executor = FakeExecutor()
    monkeypatch.setattr(workspace_routes, "executor_client", executor)
    workspace_id = create_workspace(client)
    headers = auth_headers("test-key-two")

    requests = [
        ("get", f"/api/v1/workspaces/{workspace_id}"),
        ("post", f"/api/v1/workspaces/{workspace_id}/start"),
        ("post", f"/api/v1/workspaces/{workspace_id}/stop"),
        ("post", f"/api/v1/workspaces/{workspace_id}/pause"),
        ("post", f"/api/v1/workspaces/{workspace_id}/resume"),
        ("delete", f"/api/v1/workspaces/{workspace_id}"),
    ]

    for method, path in requests:
        response = getattr(client, method)(path, headers=headers)
        assert response.status_code == 404

    assert executor.calls == []
