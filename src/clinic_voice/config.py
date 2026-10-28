from functools import lru_cache
from uuid import UUID

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    tool_token: SecretStr
    clinic_id: UUID

    @field_validator("tool_token")
    @classmethod
    def strong_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32 or value.get_secret_value().startswith("replace-"):
            raise ValueError("TOOL_TOKEN must be a generated secret of at least 32 characters")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
