"""
Shared test fixtures.

Two things every test needs: a client that talks to the app in-process, and a guarantee
that the app's startup actually ran so the DB engine and Redis pool exist.
"""

import socket
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url

from eap.api.health import reset_readiness_cache
from eap.core.config import get_settings
from eap.core.db import dispose_engine, dispose_owner_engine
from eap.main import app


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail in two seconds if Postgres isn't listening, not in four minutes.

    Every DB test otherwise waits out its own connect timeout, so a stopped
    Docker costs ~4 minutes to learn one fact.
    """
    url = make_url(get_settings().database_url)
    probe = socket.socket()
    probe.settimeout(2)
    try:
        probe.connect((url.host or "localhost", url.port or 5432))
    except OSError:
        pytest.exit(
            f"Postgres is not accepting connections on "
            f"{url.host}:{url.port}. Is Docker running? "
            f"Try: docker compose up -d",
            returncode=1,
        )
    finally:
        probe.close()


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """
    Force APP_ENV=test for every test.

    Settings is @lru_cache'd, so we clear the cache on both sides - otherwise the first
    test to import config would freeze dev settings for the whole session.
    """
    monkeypatch.setenv("APP_ENV", "test")
    reset_readiness_cache()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests() -> AsyncIterator[None]:
    """
    Tear down the process-wide engine after every test.

    eap.core.db keeps one engine per process, which is right in production and wrong here:
    pytest-asyncio gives each test its own event loop, and an asyncpg connection created in
    one loop but finalised in another is undefined behaviour - it surfaces as
    "coroutine Connection._cancel was never awaited" in whichever test runs next.
    """
    yield
    await dispose_engine()
    await dispose_owner_engine()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """
    An httpx client bound to the ASGI app - no socket, no running server.

    Function-scoped on purpose: asyncpg connections belong to the event loop that made
    them, and each test gets a fresh loop.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
