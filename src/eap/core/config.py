"""
Settings - one object, read once from the environment / .env.

pydantic-settings validates types at startup, so a bad DATABASE_URL fails loudly at boot
instead of at the first request. Nothing else in the codebase reads os.environ directly.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "eap"
    log_level: str = "INFO"

    database_url: PostgresDsn = Field(default="postgresql+asyncpg://eap:eap@localhost:5432/eap")
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")

    # The RUNNING APP connects as a restricted role that owns no tables, so every RLS
    # policy applies to it. database_url stays the owner, used by migrations and seeds.
    # Superusers bypass RLS entirely - if the app used database_url, isolation would be
    # switched off in production while every isolation test still passed.
    app_database_url: PostgresDsn | None = None
    # Auth (M1). The default is a dev placeholder; production must override it.
    jwt_secret: str = "dev-only-insecure-secret-replace-in-every-real-environment"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30

    # Agents (M2). v1 runs synchronously, so this is also the request's worst case. When
    # M4 moves runs onto a Redis queue this becomes the worker's budget instead.
    agent_run_timeout_seconds: int = 60

    # LLM APIs (used from M2)
    google_api_key: str = ""

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "Settings":
        # HS256 signs with the raw secret, so a short one is brute-forceable offline from a
        # single captured token - and whoever cracks it can mint a token for any tenant.
        if len(self.jwt_secret.encode()) < 32:
            raise ValueError("jwt_secret must be at least 32 bytes")
        if self.is_prod and self.jwt_secret.startswith("dev-only"):
            raise ValueError("jwt_secret must be set explicitly in production")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached so every request shares one parsed Settings; tests clear the cache to override."""
    return Settings()
