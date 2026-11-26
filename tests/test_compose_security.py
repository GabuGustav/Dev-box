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
