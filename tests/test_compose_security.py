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
