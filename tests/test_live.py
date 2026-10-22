"""
Live smoke tests: real model, real platform, real bill. Opt in with --live.

Everything else in this suite fakes the model on purpose: deterministic, free, offline.
That leaves one class of bug invisible - the real vendor disagreeing with our assumptions.
The first live call (EAP-50) found one within minutes: the 2.x SDK had dropped
`temperature`, and every fake had happily accepted it. These tests exist to find the next
one before a tenant does.

    python -m pytest --live -m live -q

They run on whatever .env names: LLM_PROVIDER and its key, plus GOOGLE_API_KEY for the
answer agent's embeddings. A full pass costs a few cents on Claude Haiku 4.5. Assertions
are loose about wording - a real model never phrases things the same way twice - and
strict about contracts: status, shape, citations, refusals, the audit trail.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.agents.answer import REFUSAL
from eap.core.config import get_settings
from eap.core.db import get_owner_session_factory, get_session_factory
from eap.core.security import hash_password
from eap.llm import anthropic as anthropic_module
from eap.llm import gemini as gemini_module
from eap.llm import provider, secrets

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("temporal_worker")]

PASSWORD = "correct-horse-battery-staple"

PLATFORM_TEXT = (
    "The Enterprise Agent Platform runs AI agents for many companies on shared infrastructure. "
    "Each company is a tenant, and Postgres row-level security keeps every tenant's rows invisible "
    "to the others, even if application code forgets a filter. Every model call is written to an "
    "append-only audit log in which each entry carries the hash of the one before it, so a deleted "
    "or edited record breaks the chain and is caught. Runs execute on Temporal, which means a "
    "worker crash or a database blip is retried automatically instead of losing the job. Calls to "
    "model vendors are wrapped in retries with jitter and a circuit breaker, so one slow provider "
    "cannot exhaust the platform. Tenants may bring their own model keys, which keeps their usage "
    "on their own bill. A tenant that brings nothing runs on the platform's default key."
)

HANDBOOK = (
    "Marina Heights resident handbook.\n\n"
    "Parking: resident parking is in the marina basement on level two. Visitor parking is beside "
    "the pool and is limited to four hours.\n\n"
    "Pool and gym: the pool is open from 6 am to 10 pm. The gym is on the ground floor and needs "
    "a resident key card.\n\n"
    "Waste: general waste is collected every morning at 8 am. Recycling goes in the blue bins by "
    "the loading bay.\n\n"
    "Deliveries: couriers leave parcels at the concierge desk in the lobby, which is staffed from "
    "7 am to 11 pm."
)

_KEY_FOR = {"gemini": "google_api_key", "anthropic": "anthropic_api_key"}


@pytest.fixture(autouse=True)
def _real_clients():
    """Build every client from the real .env, and leave none behind for the fake-model tests."""
    caches = (
        get_settings,
        gemini_module.get_llm,
        gemini_module.get_embedder,
        gemini_module._llm_breaker,
        gemini_module._embed_breaker,
        anthropic_module.get_anthropic_llm,
        anthropic_module._breaker,
        provider._tenant_gemini,
        provider._tenant_anthropic,
        secrets.get_secret_resolver,
    )
    for cached in caches:
        cached.cache_clear()
    s = get_settings()
    if not getattr(s, _KEY_FOR[s.llm_provider]):
        pytest.fail(f"--live needs a real key: LLM_PROVIDER={s.llm_provider} has none in .env")
    yield
    for cached in caches:
        cached.cache_clear()


def _needs_embeddings() -> None:
    if not get_settings().google_api_key:
        pytest.skip("the answer agent embeds with Gemini: set GOOGLE_API_KEY in .env")


async def _seed() -> dict[str, str]:
    """One tenant, one user, all three model agents enabled."""
    slug = f"live-{uuid.uuid4().hex[:8]}"
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
        for name, display in (
            ("summarize", "Summarize"),
            ("answer", "Answer from documents"),
            ("docwriter", "Document Writer"),
        ):
            await s.execute(
                text(
                    "INSERT INTO agents (id, tenant_id, name, display_name) "
                    "VALUES (gen_random_uuid(), :tid, :name, :display)"
                ),
                {"tid": tid, "name": name, "display": display},
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


async def _run(client: AsyncClient, token: str, agent: str, payload: dict) -> dict:
    """Enqueue, then poll: real models are slower than fakes, and no sync budget should decide."""
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(f"/v1/agents/{agent}/run?async=true", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    deadline = asyncio.get_running_loop().time() + 120
    while asyncio.get_running_loop().time() < deadline:
        r = await client.get(f"/v1/runs/{run_id}", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("succeeded", "failed", "timed_out"):
            return body
        await asyncio.sleep(0.5)
    pytest.fail(f"{agent} run {run_id} never finished")


async def _llm_calls(tenant_id: str, run_id: str) -> list[dict]:
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        rows = await s.execute(
            text(
                "SELECT payload FROM audit_log WHERE run_id = :rid AND event_type = 'llm.call' ORDER BY seq"
            ),
            {"rid": uuid.UUID(run_id)},
        )
        return [row.payload for row in rows]


async def test_summarize_with_a_real_model(client: AsyncClient) -> None:
    seed = await _seed()
    token = await _token(client, seed)

    run = await _run(client, token, "summarize", {"text": PLATFORM_TEXT, "max_sentences": 2})

    assert run["status"] == "succeeded", run
    summary = run["output"]["summary"]
    assert 20 < len(summary) < len(PLATFORM_TEXT), summary  # said something, and said it shorter
    # The audit trail holds the real exchange: what was asked, and what came back.
    calls = await _llm_calls(seed["tenant_id"], run["run_id"])
    assert len(calls) == 1, calls
    assert "row-level security" in calls[0]["prompt"]
    assert calls[0]["completion"].strip()


async def test_answer_cites_the_document_it_used(client: AsyncClient) -> None:
    _needs_embeddings()
    token = await _token(client, await _seed())
    r = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Marina handbook", "body": HANDBOOK, "external_id": "live:handbook"},
    )
    assert r.status_code == 201, r.text

    run = await _run(client, token, "answer", {"question": "Where should residents park their cars?"})

    assert run["status"] == "succeeded", run
    out = run["output"]
    assert out["answered"] is True, out
    assert out["citations"], out  # a grounded answer names its sources
    assert {c["document_title"] for c in out["citations"]} == {"Marina handbook"}
    assert any(w in out["answer"].lower() for w in ("basement", "level two", "level 2")), out["answer"]


async def test_answer_refuses_what_the_documents_do_not_cover(client: AsyncClient) -> None:
    _needs_embeddings()
    token = await _token(client, await _seed())
    r = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Marina handbook", "body": HANDBOOK, "external_id": "live:handbook"},
    )
    assert r.status_code == 201, r.text

    run = await _run(client, token, "answer", {"question": "What time does the city metro open on Sundays?"})

    # Either the similarity floor stops it before any model call, or the model says
    # NOT_COVERED - both must end as the same refusal, never as an invented timetable.
    assert run["status"] == "succeeded", run
    assert run["output"] == {"answer": REFUSAL, "answered": False, "citations": []}, run["output"]


async def test_docwriter_with_a_real_model(client: AsyncClient) -> None:
    token = await _token(client, await _seed())

    run = await _run(
        client,
        token,
        "docwriter",
        {
            "ticket_id": "LIVE-1",
            "title": "Add login rate limiting",
            "description": (
                "Limit failed logins to 5 per minute per tenant. Return 429 with a Retry-After "
                "header, and record every lockout in the audit trail."
            ),
        },
    )

    # Outline, draft, review - three real calls, and the review's APPROVE/REVISE line has
    # to parse. A model that rambles past the format shows up here, not in front of a user.
    assert run["status"] == "succeeded", run
    out = run["output"]
    assert out["outline"].strip(), out
    assert len(out["document"]) > 200, out
