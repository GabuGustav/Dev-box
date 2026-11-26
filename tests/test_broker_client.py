import httpx
import pytest

from worker.broker_client import (
    BrokerClient,
    BrokerNotFound,
    BrokerRejected,
    BrokerUnavailable,
)


def make_client(handler, token="b" * 48):
    client = BrokerClient(
        base_url="http://broker:9100",
        token=token,
    )
    client._client.close()
    client._client = httpx.Client(
        base_url="http://broker:9100",
        headers={"Authorization": f"Bearer {token}"},
        transport=httpx.MockTransport(handler),
    )
    return client


def test_broker_client_sends_bearer_token_and_parses_workspace():
    workspace_id = "dbx_abcdefghijklmnopqrstuv"

    def handler(request):
        assert request.headers["Authorization"] == f"Bearer {'b' * 48}"
        assert request.method == "POST"
        assert request.url.path == f"/internal/workspaces/{workspace_id}/start"
        return httpx.Response(
            200,
            json={
                "workspace_id": workspace_id,
                "container_id": "container-1",
                "container_name": "devbox-ws-test",
                "storage_ref": "xfs-test",
                "state": "running",
                "attrs": {
                    "Config": {"Labels": {"devbox.managed": "true"}},
                    "Mounts": [{"Destination": "/workspace", "Source": "/srv/ws/test"}],
                },
            },
        )

    client = make_client(handler)
    container = client.start_workspace(workspace_id)

    assert container.id == "container-1"
    assert container.status == "running"
    assert container.attrs["Mounts"][0]["Source"] == "/srv/ws/test"
    client.close()


def test_broker_client_maps_missing_resource():
    client = make_client(lambda request: httpx.Response(404, json={"detail": "missing"}))
    with pytest.raises(BrokerNotFound):
        client.get_workspace("dbx_abcdefghijklmnopqrstuv")
    client.close()


def test_broker_client_preserves_rejected_status_and_detail():
    client = make_client(
        lambda request: httpx.Response(422, json={"detail": "invalid Git operation"})
    )
    with pytest.raises(BrokerRejected) as error:
        client.run_git_helper("dbx_abcdefghijklmnopqrstuv", ["sh", "-c", "id"])
    assert error.value.status_code == 422
    assert error.value.detail == "invalid Git operation"
    client.close()


def test_broker_client_maps_server_failure_to_unavailable():
    client = make_client(
        lambda request: httpx.Response(503, json={"detail": "Docker daemon unavailable"})
    )
    with pytest.raises(BrokerUnavailable):
        client.ping()
    client.close()


def test_broker_client_rejects_missing_or_placeholder_tokens():
    with pytest.raises(RuntimeError, match="DEVBOX_BROKER_TOKEN"):
        BrokerClient(base_url="http://broker:9100", token="short").validate_config()

    with pytest.raises(RuntimeError, match="DEVBOX_BROKER_TOKEN"):
        BrokerClient(
            base_url="http://broker:9100",
            token="replace-this-with-a-different-long-random-secret",
        ).validate_config()

    BrokerClient(
        base_url="http://broker:9100",
        token="x" * 48,
    ).validate_config()

def test_broker_client_preserves_gateway_timeout():
    client = make_client(
        lambda request: httpx.Response(504, json={"detail": "Git operation timed out"})
    )
    with pytest.raises(BrokerRejected) as error:
        client.run_git_helper("dbx_abcdefghijklmnopqrstuv", ["status", "--short", "--branch"])
    assert error.value.status_code == 504
    client.close()

def test_broker_client_validates_authenticated_readiness():
    def handler(request):
        assert request.url.path == "/internal/ready"
        assert request.headers["Authorization"] == f"Bearer {'b' * 48}"
        return httpx.Response(200, json={"status": "ready"})

    client = make_client(handler)
    assert client.validate_connection() == {"status": "ready"}
    client.close()


def test_broker_client_delegates_restore_commit_and_recovery():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"workspace_id": "dbx_abcdefghijklmnopqrstuv", "state": "restored"})

    client = make_client(handler)
    workspace_id = "dbx_abcdefghijklmnopqrstuv"
    client.commit_storage_restore("workspace", workspace_id)
    client.recover_storage_restore("workspace", workspace_id)

    assert calls == [
        ("POST", f"/internal/storage/workspace/{workspace_id}/commit-restore"),
        ("POST", f"/internal/storage/workspace/{workspace_id}/recover-restore"),
    ]
    client.close()
