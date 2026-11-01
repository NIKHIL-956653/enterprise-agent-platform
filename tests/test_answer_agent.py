"""
Answer agent through the full HTTP path: retrieval real (fake embedder), model scripted.

The point of this file is the refuse path. Retrieval always returns SOMETHING unless a
floor stops it, and a model given weak context will answer anyway. So the tests assert not
just what comes back but whether the model was called at all: a refusal that still paid for
a completion is a bug.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.agents import runner
from eap.agents.answer import NOT_COVERED, REFUSAL, strip_preamble
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.rag import ingest

# Bag-of-words fake: shared words -> high cosine, disjoint words -> ~0. Defined once, there.
from tests.test_retrieval import RankingFakeEmbedder

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "correct-horse-battery-staple"
PARKING = "Resident parking is in the marina basement on level two. Visitor parking is beside the pool."
ON_TOPIC = "Where is resident parking in the marina?"
OFF_TOPIC = "When does the metro open?"


class ScriptedLLM:
    """Returns scripted completions in order and records every prompt it was given."""

    def __init__(self, *completions: str) -> None:
        self._completions = list(completions)
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        self.prompts.append(prompt)
        assert self._completions, "model called more times than scripted"
        return self._completions.pop(0)


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


async def _seed_tenant_with_answer() -> dict[str, str]:
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
                "VALUES (gen_random_uuid(), :tid, 'answer', 'Answer from documents')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return {"slug": slug, "email": email}


async def _token(client: AsyncClient, seed: dict[str, str]) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed["email"], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _ingest(client: AsyncClient, token: str, title: str, body: str) -> None:
    r = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": title, "body": body, "external_id": f"doc:{title}"},
    )
    assert r.status_code == 201, r.text


async def _ask(client: AsyncClient, token: str, question: str) -> dict:
    r = await client.post(
        "/v1/agents/answer/run",
        json={"question": question},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_answers_from_retrieved_chunks_with_citations(client, monkeypatch) -> None:
    llm = ScriptedLLM("Resident parking is in the marina basement, level two [1].")
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    token = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token, "Parking", PARKING)

    body = await _ask(client, token, ON_TOPIC)

    assert body["status"] == "succeeded", body
    out = body["output"]
    assert out["answered"] is True
    assert out["answer"] == "Resident parking is in the marina basement, level two [1]."
    assert len(out["citations"]) == 1
    cite = out["citations"][0]
    assert cite["passage"] == 1
    assert cite["document_title"] == "Parking"
    assert cite["external_id"] == "doc:Parking"
    assert cite["similarity"] > 0.5
    assert len(llm.prompts) == 1


async def test_refuses_without_a_model_call_when_nothing_clears_the_floor(client, monkeypatch) -> None:
    llm = ScriptedLLM()  # nothing scripted: any call is a failure
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    token = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token, "Parking", PARKING)

    body = await _ask(client, token, OFF_TOPIC)

    assert body["status"] == "succeeded", body
    assert body["output"] == {"answer": REFUSAL, "answered": False, "citations": []}
    assert llm.prompts == []


async def test_prompt_fences_passages_and_question(client, monkeypatch) -> None:
    llm = ScriptedLLM("Level two [1].")
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    token = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token, "Parking", PARKING)

    await _ask(client, token, ON_TOPIC)

    prompt = llm.prompts[0]
    assert prompt.index('<passage id="1">') < prompt.index(PARKING) < prompt.index("</passage>")
    assert prompt.index("<question>") < prompt.index(ON_TOPIC) < prompt.index("</question>")
    # Rules come before any untrusted text.
    assert prompt.index("Rules:") < prompt.index("<passage")


async def test_model_saying_not_covered_becomes_a_refusal(client, monkeypatch) -> None:
    llm = ScriptedLLM(f"{NOT_COVERED}.")
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    token = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token, "Parking", PARKING)

    body = await _ask(client, token, "How much does marina parking cost?")

    assert body["status"] == "succeeded", body
    assert body["output"]["answered"] is False
    assert body["output"]["answer"] == REFUSAL
    assert body["output"]["citations"] == []
    assert len(llm.prompts) == 1  # on-topic chunks were retrieved, so the model WAS asked


async def test_hits_but_no_model_fails_loudly(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: None)
    token = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token, "Parking", PARKING)

    body = await _ask(client, token, ON_TOPIC)

    assert body["status"] == "failed"
    assert "no language model" in body["error"]


async def test_another_tenant_asking_the_exact_words_gets_a_refusal(client, monkeypatch) -> None:
    llm = ScriptedLLM()
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    token_a = await _token(client, await _seed_tenant_with_answer())
    token_b = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token_a, "Parking", PARKING)

    body = await _ask(client, token_b, PARKING)

    assert body["status"] == "succeeded", body
    assert body["output"]["answered"] is False
    assert body["output"]["citations"] == []
    assert llm.prompts == []


# --- D7: no "According to the passage" ----------------------------------------------------
# The phrase reads like a homework answer, and it varied between two identical live runs, so
# it is model variance, not a string in our code. The prompt forbids it; strip_preamble makes
# the guarantee deterministic. The live suite (pytest --live) checks the prompt on a real model.


async def test_prompt_forbids_mentioning_the_passages(client, monkeypatch) -> None:
    llm = ScriptedLLM("Level two [1].")
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    token = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token, "Parking", PARKING)

    await _ask(client, token, ON_TOPIC)

    prompt = llm.prompts[0]
    rules = prompt[prompt.index("Rules:") : prompt.index("<passage")]
    assert "Never mention the passages" in rules
    assert "According to the passage" in rules  # named, so the model knows what not to write


async def test_an_according_to_the_passage_opener_never_reaches_the_reader(client, monkeypatch) -> None:
    llm = ScriptedLLM("According to the passage, resident parking is in the marina basement [1].")
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    token = await _token(client, await _seed_tenant_with_answer())
    await _ingest(client, token, "Parking", PARKING)

    out = (await _ask(client, token, ON_TOPIC))["output"]

    assert out["answered"] is True
    assert "according to the passage" not in out["answer"].lower()
    assert out["answer"] == "Resident parking is in the marina basement [1]."
    assert [c["passage"] for c in out["citations"]] == [1]


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        ("According to the passage, the pool opens at 6 [1].", "The pool opens at 6 [1]."),
        ("According to the passages, the pool opens at 6 [1].", "The pool opens at 6 [1]."),
        ("according to the provided context: the pool opens at 6 [1].", "The pool opens at 6 [1]."),
        ("Based on the provided passages, the fee is 20 AED [2].", "The fee is 20 AED [2]."),
        ("Based solely on the documents, the fee is 20 AED [2].", "The fee is 20 AED [2]."),
        ("The passage states that the pool opens at 6 [1].", "The pool opens at 6 [1]."),
        ("The passages indicate the gym closes at 10 [3].", "The gym closes at 10 [3]."),
    ],
)
def test_openers_are_stripped_and_the_answer_recapitalised(completion: str, expected: str) -> None:
    assert strip_preamble(completion) == expected


@pytest.mark.parametrize(
    "completion",
    [
        "Resident parking is on level two [1].",  # already direct
        "The ferry passage takes 20 minutes [1].",  # 'passage' as a word, not the opener
        "According to passage [2], the gate code changes monthly.",  # carries a citation: keep it
        "Visitors park by the pool; according to the passage on fees, it is free [1].",  # not leading
        "According to the passage,",  # nothing would be left: keep the original
    ],
)
def test_anything_else_is_left_exactly_as_written(completion: str) -> None:
    assert strip_preamble(completion) == completion
