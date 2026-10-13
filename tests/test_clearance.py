"""
Document clearance (EAP-40).

The point of this file is that clearance filters at RETRIEVAL, not at display. A chunk the
caller is not cleared for is never returned, so it never reaches a prompt, so no downstream
check has to un-see it. The last test is the one that matters: the answer agent refuses
rather than citing a document the asker cannot read, and it does so without an LLM call.

Two users share one tenant and one corpus. Everything these tests prove is about the
difference between them, not about tenant isolation - that is EAP-35's job.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.agents import runner
from eap.core.db import get_owner_session_factory, get_session_factory
from eap.core.security import hash_password
from eap.rag import ingest
from eap.rag.retrieval import retrieve

# Bag-of-words over a fixed vocabulary, L2-normalised. Defined once, in test_retrieval.
from tests.test_retrieval import RankingFakeEmbedder

PASSWORD = "correct-horse-battery-staple"

# Both mention marina and parking, so both are near-identical to the query below. Which one
# comes back is decided by clearance, not by similarity - that is the whole experiment.
PUBLIC_TEXT = "Visitor parking at the marina is beside the pool and open to every resident."
RESTRICTED_TEXT = "Marina parking level two is reserved for board members and the beach gate code."
QUESTION = "Where is marina parking?"

PUBLIC = 0
RESTRICTED = 3


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


class ScriptedLLM:
    """Returns scripted completions in order and records every prompt it was given."""

    def __init__(self, *completions: str) -> None:
        self._completions = list(completions)
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        self.prompts.append(prompt)
        assert self._completions, "model called more times than scripted"
        return self._completions.pop(0)


async def _seed_tenant() -> dict[str, str]:
    """One tenant, two users at different clearances, the answer agent enabled."""
    slug = f"t-{uuid.uuid4().hex[:8]}"
    low = f"low-{uuid.uuid4().hex[:8]}@example.com"
    high = f"high-{uuid.uuid4().hex[:8]}@example.com"
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
        for email, clearance in ((low, PUBLIC), (high, RESTRICTED)):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, clearance) "
                    "VALUES (gen_random_uuid(), :tid, :email, :ph, :clr)"
                ),
                {"tid": tid, "email": email, "ph": hash_password(PASSWORD), "clr": clearance},
            )
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'answer', 'Answer from documents')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return {"slug": slug, "low": low, "high": high, "tenant_id": str(tid)}


async def _token(client: AsyncClient, seed: dict[str, str], who: str) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed[who], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _ingest(client: AsyncClient, token: str, title: str, body: str, classification: int):
    return await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "title": title,
            "body": body,
            "external_id": f"doc:{title}",
            "classification": classification,
        },
    )


async def _seed_corpus(client: AsyncClient, seed: dict[str, str]) -> str:
    """Both documents, written by the cleared user. Returns the low user's token."""
    high = await _token(client, seed, "high")
    assert (await _ingest(client, high, "Visitors", PUBLIC_TEXT, PUBLIC)).status_code == 201
    assert (await _ingest(client, high, "Board", RESTRICTED_TEXT, RESTRICTED)).status_code == 201
    return await _token(client, seed, "low")


async def _retrieve(tenant_id: str, clearance: int | None, **kwargs):
    """
    The same path a request takes: restricted role, transaction-local context, RLS scoping.

    clearance=None deliberately leaves app.clearance unset, which is the case the policy's
    COALESCE covers.
    """
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        if clearance is not None:
            await s.execute(text("SELECT set_config('app.clearance', :c, true)"), {"c": str(clearance)})
        return await retrieve(session=s, **kwargs)


async def test_a_cleared_reader_sees_both_documents(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    await _seed_corpus(client, seed)

    hits = await _retrieve(seed["tenant_id"], RESTRICTED, query=QUESTION)

    assert {h.document_title for h in hits} == {"Visitors", "Board"}


async def test_an_uncleared_reader_sees_only_the_public_one(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    await _seed_corpus(client, seed)

    hits = await _retrieve(seed["tenant_id"], PUBLIC, query=QUESTION)

    # Not ranked lower - absent. The restricted chunk never enters the result set, so
    # nothing downstream has to decide whether to show it.
    assert [h.document_title for h in hits] == ["Visitors"]
    assert all("board members" not in h.content for h in hits)


async def test_a_session_with_no_clearance_set_sees_only_public(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    await _seed_corpus(client, seed)

    hits = await _retrieve(seed["tenant_id"], None, query=QUESTION)

    # Unset reads as 0, not as NULL. NULL would make every comparison null and return an
    # empty list, which a caller would misread as "nothing matched".
    assert [h.document_title for h in hits] == ["Visitors"]


async def test_classifying_above_your_own_clearance_is_refused(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    low = await _token(client, seed, "low")

    r = await _ingest(client, low, "Sneaky", RESTRICTED_TEXT, RESTRICTED)

    # Refused by the RLS WITH CHECK, not by a rule in the handler. Writing a document you
    # could not then read is a privilege bug either way.
    assert r.status_code == 403, r.text
    assert "clearance" in r.json()["detail"]


async def test_reclassifying_a_document_cascades_to_its_chunks(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    high = await _token(client, seed, "high")
    assert (await _ingest(client, high, "Visitors", PUBLIC_TEXT, PUBLIC)).status_code == 201

    # Visible to the uncleared reader while it is public.
    assert len(await _retrieve(seed["tenant_id"], PUBLIC, query=QUESTION)) == 1

    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": seed["tenant_id"]})
        await s.execute(text("SELECT set_config('app.clearance', '3', true)"))
        await s.execute(text("UPDATE documents SET classification = 3 WHERE title = 'Visitors'"))
        chunk_levels = (
            (await s.execute(text("SELECT DISTINCT classification FROM document_chunks"))).scalars().all()
        )
        await s.commit()

    # The trigger moved the chunks with the document. Without it they would sit at 0 and
    # retrieval would go on serving a document that is no longer public.
    assert chunk_levels == [3]
    assert await _retrieve(seed["tenant_id"], PUBLIC, query=QUESTION) == []


async def test_the_answer_agent_refuses_rather_than_citing_what_you_cannot_read(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await _seed_tenant()
    high = await _token(client, seed, "high")
    assert (await _ingest(client, high, "Board", RESTRICTED_TEXT, RESTRICTED)).status_code == 201

    llm = ScriptedLLM()  # nothing scripted: any model call fails the test
    monkeypatch.setattr(runner, "_default_llm", lambda: llm)
    low = await _token(client, seed, "low")

    r = await client.post(
        "/v1/agents/answer/run",
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {low}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["output"]["answered"] is False
    assert body["output"]["citations"] == []
    # The whole claim in one line: the restricted text never reached a prompt, because no
    # prompt was ever built.
    assert llm.prompts == []
