from tests.conftest import auth_headers


def test_workspace_requires_authentication(client):
    response = client.get("/api/v1/workspaces", headers={"X-DevBox-Key": ""})
    assert response.status_code == 401


def test_create_and_list_workspace(client):
    response = client.post(
        "/api/v1/workspaces",
        json={
            "name": "DevBox API",
            "repository_url": "https://github.com/GabuGustav/Dev-box.git",
            "branch": "main",
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["id"].startswith("dbx_")
    assert created["repository_url"] == "https://github.com/GabuGustav/Dev-box"

    listed = client.get("/api/v1/workspaces")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["id"] == created["id"]


def test_workspace_isolation_between_principals(client):
    created = client.post(
        "/api/v1/workspaces",
        json={
            "name": "Private workspace",
            "repository_url": "https://github.com/example/project",
        },
    ).json()

    response = client.get(
        f"/api/v1/workspaces/{created['id']}",
        headers=auth_headers("test-key-two"),
    )
    assert response.status_code == 404
    listed = client.get("/api/v1/workspaces", headers=auth_headers("test-key-two"))
    assert listed.status_code == 200
    assert listed.json() == []


def test_rejects_repository_credentials(client):
    response = client.post(
        "/api/v1/workspaces",
        json={
            "name": "Unsafe URL",
            "repository_url": "https://user:password@github.com/example/project",
        },
    )
    assert response.status_code == 422
