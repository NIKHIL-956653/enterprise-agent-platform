"""
GET /v1/runs/{id}/audit (EAP-53): the chain, re-verified on every read.

Migration 0009 ends with: "The chain, not a trigger, is what catches an owner-level edit."
This endpoint is the catching. The last two tests are the point - an edited payload and a
deleted event both turn chain_ok false, which is the difference between keeping an audit
log and being able to PROVE nobody rewrote it.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.agents import runner
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from tests.test_audit_capture import FakeLLM

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "correct-horse-battery-staple"
TEXT_IN = (
    "The platform runs many agents for many tenants. Every model call lands in a hash-chained "
    "audit log, and this test is about proving that chain still holds."
)


async def _seed_tenant() -> dict[str, str]:
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
                "VALUES (gen_random_uuid(), :tid, 'summarize', 'Summarize')"
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


async def _run_summarize(client: AsyncClient, token: str) -> str:
    r = await client.post(
        "/v1/agents/summarize/run",
        json={"text": TEXT_IN},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded", body
    return body["run_id"]


async def _audit(client: AsyncClient, token: str, run_id: str):
    return await client.get(f"/v1/runs/{run_id}/audit", headers={"Authorization": f"Bearer {token}"})


async def _tamper(tenant_id: str, sql: str, params: dict) -> None:
    """Owner-level history edit: exactly the actor the chain exists to catch. FORCE RLS
    applies to the owner too, so even the attacker has to name the tenant."""
    async with get_owner_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        await s.execute(text(sql), params)
        await s.commit()


async def test_a_clean_run_verifies_end_to_end(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM())
    seed = await _seed_tenant()
    token = await _token(client, seed)
    run_id = await _run_summarize(client, token)

    r = await _audit(client, token, run_id)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chain_ok"] is True
    types = [e["event_type"] for e in body["events"]]
    assert types[0] == "run.started"
    assert "llm.call" in types
    assert types[-1] == "run.finished"
    for e in body["events"]:
        assert e["intact"] is True
        assert len(e["hash"]) == 64 and len(e["prev_hash"]) == 64


async def test_another_tenants_run_is_404(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM())
    seed_a = await _seed_tenant()
    run_id = await _run_summarize(client, await _token(client, seed_a))

    seed_b = await _seed_tenant()
    r = await _audit(client, await _token(client, seed_b), run_id)

    # 404, not 403: confirming the run exists would already be a leak.
    assert r.status_code == 404


async def test_an_unknown_run_is_404(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM())
    seed = await _seed_tenant()
    r = await _audit(client, await _token(client, seed), str(uuid.uuid4()))
    assert r.status_code == 404


async def test_an_edited_payload_breaks_the_chain(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM())
    seed = await _seed_tenant()
    token = await _token(client, seed)
    run_id = await _run_summarize(client, token)

    await _tamper(
        seed["tenant_id"],
        'UPDATE audit_log SET payload = payload || \'{"prompt": "nothing to see here"}\'::jsonb '
        "WHERE run_id = :rid AND event_type = 'llm.call'",
        {"rid": run_id},
    )

    body = (await _audit(client, token, run_id)).json()
    assert body["chain_ok"] is False
    # Exactly the edited event fails to recompute; its neighbours still verify, which is
    # what points an investigator at the precise row that was touched.
    bad = [e["event_type"] for e in body["events"] if not e["intact"]]
    assert bad == ["llm.call"]


async def test_a_deleted_event_breaks_the_linkage(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM())
    seed = await _seed_tenant()
    token = await _token(client, seed)
    run_id = await _run_summarize(client, token)

    await _tamper(
        seed["tenant_id"],
        "DELETE FROM audit_log WHERE run_id = :rid AND event_type = 'llm.call'",
        {"rid": run_id},
    )

    body = (await _audit(client, token, run_id)).json()
    # The deleted event is gone, but it left a hole: the next event's prev_hash points at a
    # hash that no longer exists. Removal is as detectable as editing.
    assert body["chain_ok"] is False
    assert "llm.call" not in [e["event_type"] for e in body["events"]]
