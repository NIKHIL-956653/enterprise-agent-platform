"""
EAP-71: scripts/seed_demo.py refuses its public dev password in production.

The dev default is in the file, the README and .env.example. One forgotten variable on a
production box would have seeded every college admin with it. In production the seed now
stops before touching the database; elsewhere it proceeds and says so.
"""

import uuid

import pytest
from scripts import seed_demo
from scripts.seed_demo import DEV_PASSWORD, college_tenants
from sqlalchemy import text

from eap.core.config import get_settings
from eap.core.db import get_owner_session_factory


def _production(monkeypatch: pytest.MonkeyPatch) -> None:
    """APP_ENV=prod with the other settings production insists on, and no DEMO_PASSWORD."""
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("JWT_SECRET", "a-test-only-secret-that-is-long-enough-for-production")
    monkeypatch.setenv("GOOGLE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["https://demo.example.edu"]')
    monkeypatch.setattr(seed_demo, "dotenv_values", lambda _path: {})
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    get_settings.cache_clear()


async def _tenants_with(suffix: str) -> int:
    async with get_owner_session_factory()() as s:
        return (
            await s.execute(text("SELECT count(*) FROM tenants WHERE slug LIKE :p"), {"p": f"%{suffix}"})
        ).scalar_one()


async def test_production_refuses_the_dev_default_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production(monkeypatch)
    suffix = f"-{uuid.uuid4().hex[:8]}"

    with pytest.raises(SystemExit, match="DEMO_PASSWORD"):
        await seed_demo.seed(college_tenants(suffix))

    assert await _tenants_with(suffix) == 0


async def test_production_seeds_with_an_explicit_password(monkeypatch: pytest.MonkeyPatch) -> None:
    _production(monkeypatch)
    monkeypatch.setenv("DEMO_PASSWORD", "a-pilot-password-nobody-published")
    suffix = f"-{uuid.uuid4().hex[:8]}"

    ids = await seed_demo.seed(college_tenants(suffix))

    assert len(ids) == 3
    assert await _tenants_with(suffix) == 3


def test_outside_production_the_default_is_allowed_but_announced(capsys: pytest.CaptureFixture[str]) -> None:
    # The autouse test environment is APP_ENV=test, which is not production.
    assert not get_settings().is_prod
    seed_demo.check_seed_password(DEV_PASSWORD)
    assert "dev default" in capsys.readouterr().err


def test_an_explicit_password_is_never_questioned(capsys: pytest.CaptureFixture[str]) -> None:
    seed_demo.check_seed_password("something-else")
    assert capsys.readouterr().err == ""
