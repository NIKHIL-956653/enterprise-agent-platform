"""
Agent run endpoint - the full chain in one test file.

Token -> tenant context -> RLS-scoped session -> registry dispatch -> recorded run.
Uses the echo agent so nothing here depends on an LLM being reachable or affordable.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

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


async def _login(client: AsyncClient, seed: dict[str, str]) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed["email"], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
async def tenant_a(client: AsyncClient) -> AsyncIterator[dict[str, str]]:
    seed = await _seed_tenant_with_echo()
    seed["token"] = await _login(client, seed)
    yield seed


async def test_run_executes_and_records(client: AsyncClient, tenant_a: dict[str, str]) -> None:
    """The demo path. Note the agent receives the tenant WITHOUT it being in the request."""
    r = await client.post(
        "/v1/agents/echo/run",
        headers={"Authorization": f"Bearer {tenant_a['token']}"},
        json={"message": "hello platform"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "succeeded"
    assert body["output"]["message"] == "hello platform"
    assert body["output"]["tenant_id"] == tenant_a["tenant_id"]
    assert body["latency_ms"] is not None
    assert body["run_id"]


async def test_run_is_persisted(client: AsyncClient, tenant_a: dict[str, str]) -> None:
    """A run must survive the request - it is the audit and cost record, not a response."""
    r = await client.post(
        "/v1/agents/echo/run",
        headers={"Authorization": f"Bearer {tenant_a['token']}"},
        json={"message": "persist me"},
    )
    run_id = r.json()["run_id"]

    async with get_owner_session_factory()() as s:
        status = (
            await s.execute(text("SELECT status FROM agent_runs WHERE id = :rid"), {"rid": run_id})
        ).scalar_one()
    assert status == "succeeded"


async def test_bad_payload_is_422_and_records_no_run(client: AsyncClient, tenant_a: dict[str, str]) -> None:
    """
    A malformed request is the caller's bug, not a failed execution.

    If it created a run row, the tenant's failure rate would be polluted by typos.
    """
    async with get_owner_session_factory()() as s:
        before = (await s.execute(text("SELECT count(*) FROM agent_runs"))).scalar_one()

    r = await client.post(
        "/v1/agents/echo/run",
        headers={"Authorization": f"Bearer {tenant_a['token']}"},
        json={"wrong_field": "x"},
    )
    assert r.status_code == 422

    async with get_owner_session_factory()() as s:
        after = (await s.execute(text("SELECT count(*) FROM agent_runs"))).scalar_one()
    assert after == before


async def test_unknown_agent_is_404(client: AsyncClient, tenant_a: dict[str, str]) -> None:
    r = await client.post(
        "/v1/agents/no-such-agent/run",
        headers={"Authorization": f"Bearer {tenant_a['token']}"},
        json={"message": "x"},
    )
    assert r.status_code == 404


async def test_run_requires_auth(client: AsyncClient) -> None:
    r = await client.post("/v1/agents/echo/run", json={"message": "x"})
    assert r.status_code == 401


async def test_tenant_b_cannot_run_tenant_a_agent(client: AsyncClient, tenant_a: dict[str, str]) -> None:
    """
    Tenant B has no echo row of its own, so B's token must not reach A's agent.

    404 rather than 403 - confirming existence would itself leak across tenants.
    """
    b = await _seed_tenant_with_echo()
    async with get_owner_session_factory()() as s:
        await s.execute(text("DELETE FROM agents WHERE tenant_id = :tid"), {"tid": b["tenant_id"]})
        await s.commit()
    b_token = await _login(client, b)

    r = await client.post(
        "/v1/agents/echo/run",
        headers={"Authorization": f"Bearer {b_token}"},
        json={"message": "x"},
    )
    assert r.status_code == 404
