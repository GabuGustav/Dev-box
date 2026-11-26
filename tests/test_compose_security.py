from pathlib import Path

import yaml


def test_compose_does_not_publish_postgres():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "ports" not in compose["services"]["db"]


def test_compose_requires_external_database_password():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "${DB_PASSWORD:?" in compose["services"]["db"]["environment"]["POSTGRES_PASSWORD"]
    database_url = compose["services"]["api"]["environment"]["DATABASE_URL"]
    assert "${DB_PASSWORD:?" in database_url


def test_compose_api_is_localhost_bound_by_default():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["api"]["ports"] == [
        "${API_BIND_ADDRESS:-127.0.0.1}:8000:8000"
    ]

def test_api_has_dedicated_internal_reverse_proxy_network():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "api_proxy" in compose["services"]["api"]["networks"]
    assert compose["networks"]["api_proxy"]["internal"] is True
    assert compose["networks"]["api_proxy"]["name"] == "devbox_api_proxy"

def test_privileged_broker_is_internal_only():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    broker = compose["services"]["broker"]
    assert "ports" not in broker
    assert broker["networks"] == ["broker_internal"]
    assert compose["networks"]["broker_internal"]["internal"] is True


def test_worker_and_broker_share_only_the_private_broker_network_for_rpc():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "broker_internal" in compose["services"]["worker"]["networks"]
    assert "broker_internal" in compose["services"]["broker"]["networks"]
    assert "broker_internal" not in compose["services"]["api"]["networks"]

def test_worker_does_not_receive_docker_socket():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    worker_mounts = compose["services"]["worker"]["volumes"]
    broker_mounts = compose["services"]["broker"]["volumes"]
    assert not any("/var/run/docker.sock" in mount for mount in worker_mounts)
    assert any("/var/run/docker.sock" in mount for mount in broker_mounts)


def test_worker_waits_for_broker_and_receives_distinct_token():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    assert worker["depends_on"]["broker"]["condition"] == "service_healthy"
    assert worker["environment"]["DEVBOX_BROKER_URL"] == "http://broker:9100"
    assert "BROKER_SERVICE_TOKEN" in worker["environment"]["DEVBOX_BROKER_TOKEN"]

def test_worker_image_does_not_install_docker_sdk():
    dockerfile = Path("worker.Dockerfile").read_text(encoding="utf-8")
    assert "docker>=" not in dockerfile.lower()
    assert "httpx>=" in dockerfile.lower()

def test_xfs_admin_capability_and_registry_volume_are_broker_only():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    broker = compose["services"]["broker"]
    assert "SYS_ADMIN" not in worker.get("cap_add", [])
    assert "SYS_ADMIN" in broker["cap_add"]
    assert any("broker_state:/var/lib/devbox-broker" in mount for mount in broker["volumes"])
    assert not any("broker_state" in mount for mount in worker["volumes"])


def test_xfs_tools_are_installed_only_in_broker_image():
    worker_dockerfile = Path("worker.Dockerfile").read_text(encoding="utf-8").lower()
    broker_dockerfile = Path("broker.Dockerfile").read_text(encoding="utf-8").lower()
    assert "xfsprogs" not in worker_dockerfile
    assert "util-linux" not in worker_dockerfile
    assert "xfsprogs" in broker_dockerfile
    assert "util-linux" in broker_dockerfile

def test_worker_is_non_root_read_only_and_capability_free():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    dockerfile = Path("worker.Dockerfile").read_text(encoding="utf-8")
    assert worker["user"] == "10001:10001"
    assert worker["read_only"] is True
    assert worker["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in worker["security_opt"]
    assert "USER 10001:10001" in dockerfile


def test_worker_has_no_default_or_external_network_route():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    api = compose["services"]["api"]
    assert "default" not in worker["networks"]
    assert "worker_api" in worker["networks"]
    assert "broker_internal" in worker["networks"]
    assert "worker_api" in api["networks"]
    assert "broker_internal" not in api["networks"]
    assert compose["networks"]["worker_api"]["internal"] is True
    assert "egress-proxy" not in worker.get("depends_on", {})
