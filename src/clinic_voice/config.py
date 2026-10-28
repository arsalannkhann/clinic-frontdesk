from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    # Only "development" relaxes development-only safety checks (e.g. worker --calendar fake).
    app_env: str = "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
