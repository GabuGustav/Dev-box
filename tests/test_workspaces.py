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


def test_repository_discovery_is_scoped_to_authenticated_principal(client):
    repository_url = "https://github.com/example/shared-project"
    first = client.post(
        "/api/v1/workspaces",
        json={"name": "Agent one's workspace", "repository_url": repository_url},
    ).json()
    second = client.post(
        "/api/v1/workspaces",
        headers=auth_headers("test-key-two"),
        json={"name": "Agent two's workspace", "repository_url": repository_url},
    ).json()

    first_results = client.get(
        "/api/v1/workspaces/discover", params={"repository_url": repository_url}
    )
    second_results = client.get(
        "/api/v1/workspaces/discover",
        params={"repository_url": repository_url},
        headers=auth_headers("test-key-two"),
    )

    assert first_results.status_code == 200
    assert [item["id"] for item in first_results.json()] == [first["id"]]
    assert second_results.status_code == 200
    assert [item["id"] for item in second_results.json()] == [second["id"]]


def test_repository_discovery_normalizes_git_suffix(client):
    created = client.post(
        "/api/v1/workspaces",
        json={
            "name": "Normalized lookup",
            "repository_url": "https://github.com/example/project.git",
        },
    ).json()

    response = client.get(
        "/api/v1/workspaces/discover",
        params={"repository_url": "https://github.com/example/project"},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [created["id"]]


def test_repository_discovery_rejects_invalid_url(client):
    response = client.get(
        "/api/v1/workspaces/discover", params={"repository_url": "not-a-repository"}
    )

    assert response.status_code == 422
