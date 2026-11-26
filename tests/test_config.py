import pytest
from pydantic import ValidationError

from app.config import Settings


def test_development_settings_allow_local_defaults():
    settings = Settings(_env_file=None, environment="development")
    assert settings.environment == "development"


def test_production_rejects_missing_executor_token():
    with pytest.raises(ValidationError, match="EXECUTOR_SERVICE_TOKEN"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://devbox:unique-secret@db:5432/devbox",
        )


def test_production_rejects_sqlite_database():
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(
            _env_file=None,
            environment="production",
            executor_service_token="x" * 48,
            database_url="sqlite:///./devbox.db",
        )


def test_production_rejects_known_placeholder_credentials():
    with pytest.raises(ValidationError, match="placeholder"):
        Settings(
            _env_file=None,
            environment="production",
            executor_service_token="x" * 48,
            database_url="postgresql+psycopg://devbox:devbox@db:5432/devbox",
        )


def test_production_accepts_non_placeholder_configuration():
    settings = Settings(
        _env_file=None,
        environment="production",
        executor_service_token="x" * 48,
        database_url="postgresql+psycopg://devbox:unique-random-secret@db:5432/devbox",
    )
    assert settings.environment == "production"
