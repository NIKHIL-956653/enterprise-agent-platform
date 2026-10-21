"""
EAP-42: every run leaves evidence, and the evidence is clean.

The wrappers live in the runner, so these tests drive real HTTP runs and then read the
trail back. What they prove: a run writes started/finished, every model call lands with
its prompt and completion, a FAILED call still records what was asked, retrieval records
which chunks at what similarity - and PII is scrubbed before any of it is stored, because
record() redacts and no call site is trusted to remember.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.agents import runner
from eap.audit import verify_chain
from eap.core.db import get_owner_session_factory, get_session_factory
from eap.core.security import hash_password
from eap.llm.base import LLMError
from eap.rag import ingest
from tests.test_retrieval import RankingFakeEmbedder

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "correct-horse-battery-staple"
# Long enough for summarize's 50-char floor, and salted with exactly the shapes the
# redactor must catch. The model may see this raw; the TRAIL must not.
PII_TEXT = (
    "Reach Jane at jane.doe@example.com about the marina booking and bill card "
    "4111 1111 1111 1111 before the pool opens on Friday."
)
PARKING = "Resident parking is in the marina basement on level two beside the pool."


class FakeLLM:
    def __init__(self, answer: str = "A canned summary.", fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise LLMError("model call failed")
        return self.answer


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


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
        for name, display in (("summarize", "Summarize"), ("answer", "Answer from documents")):
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
    r = await client.post(
        f"/v1/agents/{agent}/run", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _events(tenant_id: str) -> list:
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        return (await s.execute(text("SELECT event_type, payload, run_id FROM audit_log ORDER BY seq"))).all()


async def test_a_run_writes_started_and_finished(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM())
    seed = await _seed_tenant()
    body = await _run(client, await _token(client, seed), "summarize", {"text": PARKING * 2})

    events = await _events(seed["tenant_id"])
    types = [e.event_type for e in events]

    assert types[0] == "run.started"
    assert types[-1] == "run.finished"
    assert {str(e.run_id) for e in events} == {body["run_id"]}
    assert events[-1].payload["status"] == "succeeded"
    assert events[-1].payload["latency_ms"] is not None


async def test_llm_calls_carry_prompt_and_completion(client, monkeypatch) -> None:
    fake = FakeLLM(answer="Parking is downstairs.")
    monkeypatch.setattr(runner, "_default_llm", lambda: fake)
    seed = await _seed_tenant()
    await _run(client, await _token(client, seed), "summarize", {"text": PARKING * 2})

    calls = [e for e in await _events(seed["tenant_id"]) if e.event_type == "llm.call"]

    assert len(calls) == 1
    assert calls[0].payload["prompt"] == fake.prompts[0]  # the RENDERED prompt, verbatim
    assert calls[0].payload["completion"] == "Parking is downstairs."


async def test_a_failed_llm_call_is_still_evidence(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM(fail=True))
    seed = await _seed_tenant()
    body = await _run(client, await _token(client, seed), "summarize", {"text": PARKING * 2})

    events = await _events(seed["tenant_id"])
    types = [e.event_type for e in events]

    assert body["status"] == "failed"
    assert "llm.error" in types  # what was ASKED survives, even though asking failed
    assert next(e for e in events if e.event_type == "llm.error").payload["prompt"]
    assert events[-1].payload["status"] == "failed"


async def test_retrieval_records_chunks_and_scores(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM(answer="Level two [1]."))
    seed = await _seed_tenant()
    token = await _token(client, seed)
    r = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "Parking", "body": PARKING, "external_id": "doc:parking"},
    )
    assert r.status_code == 201, r.text
    await _run(client, token, "answer", {"question": "Where is marina parking?"})

    hits = [e for e in await _events(seed["tenant_id"]) if e.event_type == "retrieval.performed"]

    assert len(hits) == 1
    assert hits[0].payload["query"] == "Where is marina parking?"
    assert len(hits[0].payload["chunks"]) == 1
    assert hits[0].payload["chunks"][0]["similarity"] > 0.5
    assert hits[0].payload["chunks"][0]["chunk_id"]


async def test_pii_never_reaches_the_trail(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM())
    seed = await _seed_tenant()
    await _run(client, await _token(client, seed), "summarize", {"text": PII_TEXT})

    events = await _events(seed["tenant_id"])
    started = next(e for e in events if e.event_type == "run.started")
    call = next(e for e in events if e.event_type == "llm.call")

    # The model saw the raw text - redaction protects the TRAIL, not the inference.
    assert started.payload["input"]["text"].count("[REDACTED:email]") == 1
    assert "[REDACTED:card]" in started.payload["input"]["text"]
    assert started.payload["_redactions"] >= 2
    for e in (started, call):
        flat = str(e.payload)
        assert "jane.doe@example.com" not in flat
        assert "4111" not in flat


async def test_the_chain_still_verifies_after_real_runs(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM())
    seed = await _seed_tenant()
    token = await _token(client, seed)
    for _ in range(3):
        await _run(client, token, "summarize", {"text": PARKING * 2})

    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": seed["tenant_id"]})
        assert await verify_chain(s) is None
