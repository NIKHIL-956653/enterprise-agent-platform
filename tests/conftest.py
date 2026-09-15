"""
Shared test fixtures.

Two things every test needs: a client that talks to the app in-process, and a guarantee
that the app's startup actually ran so the DB engine and Redis pool exist.
"""

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from eap.core.config import get_settings
from eap.main import app


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """
    Force APP_ENV=test for every test.

    Settings is @lru_cache'd, so we clear the cache on both sides — otherwise the first
    test to import config would freeze dev settings for the whole session.
    Real env vars beat .env in pydantic-settings, so this wins over your .env file.
    """
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """
    An httpx client bound to the ASGI app — no socket, no running server.

    Function-scoped on purpose. asyncpg connections belong to the event loop that made
    them, and pytest-asyncio gives each test a fresh loop. A session-scoped engine would
    be reused across loops and blow up with "attached to a different loop". Because
    lifespan's dispose_engine() resets the module-level engine to None, each test builds
    a clean one.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
