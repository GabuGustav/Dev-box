from functools import lru_cache

from pydantic import model_validator
from sqlalchemy.engine import make_url
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "DevBox"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./devbox.db"
    api_v1_prefix: str = "/api/v1"
    executor_service_url: str = "http://worker:9000"
    executor_service_token: str = ""
    lifecycle_reconcile_interval_seconds: int = 60

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @model_validator(mode="after")
    def validate_production_security(self):
        if self.environment.strip().lower() in {"production", "prod"}:
            token = self.executor_service_token.strip()
            if len(token) < 32 or token.lower() in {
                "replace-this-with-a-long-random-secret",
                "changeme",
                "change-me",
                "devbox",
            }:
                raise ValueError(
                    "Production requires EXECUTOR_SERVICE_TOKEN with at least 32 "
                    "non-placeholder characters."
                )
            database_url = self.database_url.lower()
            if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("Production requires a PostgreSQL DATABASE_URL.")
            parsed_database_url = make_url(self.database_url)
            if not parsed_database_url.host or not parsed_database_url.password:
                raise ValueError("Production DATABASE_URL requires a database host and password.")
            if len(parsed_database_url.password) < 24:
                raise ValueError("Production database password must contain at least 24 characters.")
            placeholder_credentials = (
                "devbox:devbox@",
                "change-me",
                "replace-with-a-unique-random-secret",
                "local-development-only",
            )
            if any(value in database_url for value in placeholder_credentials):
                raise ValueError("Production DATABASE_URL contains placeholder credentials.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
