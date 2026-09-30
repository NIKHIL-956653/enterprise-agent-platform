"""
Per-tenant rate limiting.

Runs against the real Redis from docker compose. Every test seeds its own tenant, so the
counters are naturally isolated and nothing here flushes a shared database.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.config import get_settings
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password

PASSWORD = "correct-horse-battery-staple"


async def _seed_tenant_with_echo() -> dict[str, str]:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, :slug) RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (gen_random_uuid(), :tid, :email, :ph)"
            ),
            {"tid": tid, "email": email, "ph": hash_password(PASSWORD)},
        )
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'echo', 'Echo')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return {"slug": slug, "email": email, "tenant_id": str(tid)}


async def _token(client: AsyncClient, seed: dict[str, str]) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed["email"], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _run(client: AsyncClient, token: str):
    return await client.post(
        "/v1/agents/echo/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "hello"},
    )


@pytest.fixture
def small_limit(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_MINUTE", "3")
    get_settings.cache_clear()
    return 3


async def test_requests_under_the_limit_pass(client: AsyncClient, small_limit: int) -> None:
    token = await _token(client, await _seed_tenant_with_echo())
    for _ in range(small_limit):
        assert (await _run(client, token)).status_code == 200


async def test_the_next_request_is_refused(client: AsyncClient, small_limit: int) -> None:
    token = await _token(client, await _seed_tenant_with_echo())
    for _ in range(small_limit):
        await _run(client, token)

    r = await _run(client, token)
    assert r.status_code == 429
    # A client that cannot tell how long to wait will simply retry immediately.
    assert int(r.headers["Retry-After"]) > 0
    assert r.headers["X-RateLimit-Remaining"] == "0"


async def test_remaining_counts_down(client: AsyncClient, small_limit: int) -> None:
    token = await _token(client, await _seed_tenant_with_echo())
    first = await _run(client, token)
    assert first.headers["X-RateLimit-Remaining"] == str(small_limit - 1)


async def test_one_tenant_cannot_exhaust_anothers_quota(client: AsyncClient, small_limit: int) -> None:
    # The whole point of per-TENANT limiting. If this fails, one noisy customer is an outage
    # for everybody else, which is exactly what a global limiter does.
    noisy = await _token(client, await _seed_tenant_with_echo())
    quiet = await _token(client, await _seed_tenant_with_echo())

    for _ in range(small_limit + 2):
        await _run(client, noisy)

    assert (await _run(client, noisy)).status_code == 429
    assert (await _run(client, quiet)).status_code == 200


async def test_zero_disables_the_limiter(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_MINUTE", "0")
    get_settings.cache_clear()
    token = await _token(client, await _seed_tenant_with_echo())

    for _ in range(5):
        assert (await _run(client, token)).status_code == 200
