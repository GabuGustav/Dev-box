from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
