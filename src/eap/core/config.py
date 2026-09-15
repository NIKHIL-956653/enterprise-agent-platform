"""
Settings — one object, read once from the environment / .env.

pydantic-settings validates types at startup, so a bad DATABASE_URL fails loudly at boot
instead of at the first request. Nothing else in the codebase reads os.environ directly.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "eap"
    log_level: str = "INFO"

    database_url: PostgresDsn = Field(default="postgresql+asyncpg://eap:eap@localhost:5432/eap")
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")

    # LLM APIs (used from M2)
    google_api_key: str = ""

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Cached so every request shares one parsed Settings; tests clear the cache to override."""
    return Settings()
