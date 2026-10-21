"""
Shared test fixtures.

Two things every test needs: a client that talks to the app in-process, and a guarantee
that the app's startup actually ran so the DB engine and Redis pool exist. Since EAP-44 a
third joins them: tests that POST a run need a Temporal worker, because the API enqueues
instead of executing - those tests opt in with
`pytestmark = pytest.mark.usefixtures("temporal_worker")`.
"""

import socket
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from temporalio.worker import Worker

from eap.api.health import reset_readiness_cache
from eap.core.config import get_settings
from eap.core.db import dispose_engine, dispose_owner_engine
from eap.core.temporal import dispose_temporal_client, get_temporal_client
from eap.main import app
from eap.workflows import AgentRunWorkflow
from eap.workflows.activities import run_agent as run_agent_activity


def _probe(host: str, port: int, what: str, hint: str) -> None:
    probe = socket.socket()
    probe.settimeout(2)
    try:
        probe.connect((host, port))
    except OSError:
        pytest.exit(
            f"{what} is not accepting connections on {host}:{port}. {hint}",
            returncode=1,
        )
    finally:
        probe.close()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Fail in two seconds if the stack isn't up, not in four minutes.

    Every DB test otherwise waits out its own connect timeout, and every run test would
    wait out a workflow timeout, so a stopped Docker costs minutes to learn one fact.
    """
    url = make_url(str(get_settings().database_url))
    _probe(url.host or "localhost", url.port or 5432, "Postgres", "Try: docker compose up -d")
    address = get_settings().temporal_address
    host, _, port = address.partition(":")
    _probe(host or "localhost", int(port or 7233), "Temporal", "Try: docker compose up -d")


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


@pytest.fixture(autouse=True)
async def _dispose_temporal_between_tests() -> AsyncIterator[None]:
    """Same disease, same cure: a gRPC channel born in a dead event loop poisons the next
    test, so the cached Temporal client dies with the loop that made it."""
    yield
    await dispose_temporal_client()


@pytest.fixture
async def temporal_worker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """
    An in-process worker on a task queue unique to this test.

    In-process is the point: monkeypatched fakes (runner._default_llm, the fake embedder)
    are plain module attributes, so the activity sees them exactly as the old in-request
    path did - tests fake the model the same way before and after the cutover. The unique
    queue keeps a crashed test's leftover workflows from being picked up by the next test.
    """
    queue = f"test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", queue)
    get_settings.cache_clear()
    client = await get_temporal_client()
    async with Worker(
        client,
        task_queue=queue,
        workflows=[AgentRunWorkflow],
        activities=[run_agent_activity],
    ):
        yield


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
