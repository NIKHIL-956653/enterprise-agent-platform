"""
EAP-44: the API hands runs to the kitchen.

Sync callers see the same envelope as before the cutover - that is the contract this file
guards. Async callers get "queued" plus an id that resolves on its own. A client that
retries with an Idempotency-Key gets the SAME run back, never a second bill. And garbage
never reaches the queue: validation failures leave no run row at all.
"""

import asyncio
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
TEXT = (
    "The platform runs many agents for many tenants. Each tenant's rows are isolated by "
    "row-level security, and every agent run is recorded whether it succeeds or fails."
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


async def _run_count(tenant_id: str) -> int:
    # Owner session on purpose: it bypasses RLS, so the count is the truth, not the view.
    async with get_owner_session_factory()() as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM agent_runs WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).scalar_one()


async def _poll_until_done(client: AsyncClient, token: str, run_id: str) -> dict:
    deadline = asyncio.get_event_loop().time() + 20
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("succeeded", "failed", "timed_out"):
            return body
        await asyncio.sleep(0.2)
    pytest.fail(f"run {run_id} never finished")


async def test_sync_run_returns_the_finished_envelope(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM(answer="Done."))
    seed = await _seed_tenant()
    token = await _token(client, seed)

    r = await client.post(
        "/v1/agents/summarize/run",
        json={"text": TEXT},
        headers={"Authorization": f"Bearer {token}"},
    )

    # The pre-cutover contract, byte for byte: the caller cannot tell the kitchen exists.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["output"]["summary"] == "Done."
    assert body["latency_ms"] is not None


async def test_async_returns_queued_and_the_run_completes(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM(answer="Done."))
    seed = await _seed_tenant()
    token = await _token(client, seed)

    r = await client.post(
        "/v1/agents/summarize/run?async=true",
        json={"text": TEXT},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert r.json()["output"] is None

    done = await _poll_until_done(client, token, r.json()["run_id"])
    assert done["status"] == "succeeded"


async def test_the_same_idempotency_key_returns_the_same_run(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM(answer="Done."))
    seed = await _seed_tenant()
    token = await _token(client, seed)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "retry-abc"}

    first = await client.post("/v1/agents/summarize/run", json={"text": TEXT}, headers=headers)
    second = await client.post("/v1/agents/summarize/run", json={"text": TEXT}, headers=headers)

    assert first.json()["run_id"] == second.json()["run_id"]
    assert second.json()["status"] == "succeeded"
    assert await _run_count(seed["tenant_id"]) == 1  # one bill, however many retries


async def test_different_keys_make_different_runs(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: FakeLLM(answer="Done."))
    seed = await _seed_tenant()
    token = await _token(client, seed)

    ids = set()
    for key in ("key-one", "key-two"):
        r = await client.post(
            "/v1/agents/summarize/run",
            json={"text": TEXT},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )
        ids.add(r.json()["run_id"])

    assert len(ids) == 2


async def test_validation_failure_enqueues_nothing(client, monkeypatch) -> None:
    fake = FakeLLM()
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    seed = await _seed_tenant()
    token = await _token(client, seed)

    r = await client.post(
        "/v1/agents/summarize/run",
        json={"text": "too short"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 422
    assert fake.prompts == []
    # Garbage never entered the queue: no run row, nothing for a worker to find.
    assert await _run_count(seed["tenant_id"]) == 0
