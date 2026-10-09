"""
Retrieval ranking, the top-k clamp, and the refuse path.

The FakeEmbedder in test_documents_api.py cannot test any of this: it returns multiples of
the all-ones vector, so cosine similarity between ANY two of its outputs is exactly 1.0 -
fine for proving ingestion stores things, useless for proving nearest-first. This fake is a
bag-of-words over a tiny vocabulary, L2-normalised, with a constant baseline component so no
vector is ever zero (pgvector yields NaN for cosine distance against a zero vector).
"""

import math
import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_owner_session_factory, get_session_factory
from eap.core.security import hash_password
from eap.models.document import EMBEDDING_DIMENSIONS
from eap.rag import ingest
from eap.rag.retrieval import MAX_TOP_K, retrieve

PASSWORD = "correct-horse-battery-staple"

# One dimension per word, one extra for the shared baseline, zeros elsewhere.
VOCAB = ("marina", "parking", "school", "beach", "pool", "metro")
_BASELINE_DIM = len(VOCAB)
_BASELINE = 0.1


class RankingFakeEmbedder:
    """Same text -> same vector; shared words -> high cosine; disjoint words -> near zero."""

    def __init__(self) -> None:
        self.query_calls: list[str] = []

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text_: str) -> list[float]:
        self.query_calls.append(text_)
        return self._vector(text_)

    def _vector(self, value: str) -> list[float]:
        words = set(re.findall(r"[a-z]+", value.lower()))
        v = [0.0] * EMBEDDING_DIMENSIONS
        v[_BASELINE_DIM] = _BASELINE
        for i, term in enumerate(VOCAB):
            if term in words:
                v[i] = 1.0
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    # Patching the MODULE attribute covers both ingestion (POST /v1/documents) and
    # retrieval, because retrieval deliberately calls ingest._default_embedder() through
    # the module rather than binding the name at import time.
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
        await s.commit()
    return {"slug": slug, "email": email, "tenant_id": str(tid)}


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


async def _retrieve(tenant_id: str, **kwargs):
    # The same path a request takes: restricted-role session, transaction-local tenant
    # context, RLS doing the scoping. set_config and retrieve share one transaction.
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        return await retrieve(session=s, **kwargs)


async def test_nearest_chunk_comes_back_first(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed)
    await _ingest(client, token, "exact", "marina parking")
    await _ingest(client, token, "partial", "marina school")
    await _ingest(client, token, "unrelated", "beach pool")

    chunks = await _retrieve(seed["tenant_id"], query="marina parking")

    # exact shares both words (~1.0), partial shares one (~0.5), unrelated shares only the
    # baseline (~0.005) and must be filtered by the floor, not merely ranked last.
    assert [c.document_title for c in chunks] == ["exact", "partial"]
    assert chunks[0].similarity > chunks[1].similarity


async def test_off_topic_query_returns_nothing(client: AsyncClient) -> None:
    # The refuse path. A vector search always HAS a nearest neighbour; without the floor
    # this would confidently return the most beach-ish parking chunk.
    seed = await _seed_tenant()
    token = await _token(client, seed)
    await _ingest(client, token, "parking", "marina parking metro")

    assert await _retrieve(seed["tenant_id"], query="beach pool") == []


async def test_the_floor_is_what_filters(client: AsyncClient) -> None:
    # Same corpus, same query, floor lowered to zero: the off-topic chunk comes back.
    # Proves the empty result above is the floor working, not the data being absent.
    seed = await _seed_tenant()
    token = await _token(client, seed)
    await _ingest(client, token, "parking", "marina parking metro")

    chunks = await _retrieve(seed["tenant_id"], query="beach pool", min_similarity=0.0)
    assert len(chunks) == 1


async def test_top_k_limits_results(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed)
    for i in range(3):
        await _ingest(client, token, f"doc{i}", f"marina parking {VOCAB[i]}")

    chunks = await _retrieve(seed["tenant_id"], query="marina parking", top_k=2)
    assert len(chunks) == 2


async def test_top_k_is_clamped_to_the_cap(client: AsyncClient) -> None:
    # 22 matching one-chunk documents, top_k=10_000: the clamp, not the caller, decides.
    seed = await _seed_tenant()
    token = await _token(client, seed)
    for i in range(MAX_TOP_K + 2):
        await _ingest(client, token, f"doc{i}", "marina parking")

    chunks = await _retrieve(seed["tenant_id"], query="marina parking", top_k=10_000)
    assert len(chunks) == MAX_TOP_K


async def test_empty_query_costs_no_embedding_call(
    client: AsyncClient, fake_embedder: RankingFakeEmbedder
) -> None:
    seed = await _seed_tenant()

    assert await _retrieve(seed["tenant_id"], query="   \n ") == []
    assert fake_embedder.query_calls == []


async def test_results_carry_citation_fields(client: AsyncClient) -> None:
    # Grounding depends on these surviving the pipeline: a claim must point back to a
    # document a human can open, not just to "a chunk".
    seed = await _seed_tenant()
    token = await _token(client, seed)
    await _ingest(client, token, "Marina guide", "marina parking metro")

    (chunk,) = await _retrieve(seed["tenant_id"], query="marina parking")
    assert chunk.document_title == "Marina guide"
    assert chunk.external_id == "doc:Marina guide"
    assert chunk.ordinal == 0
    assert "marina" in chunk.content
    assert 0.0 < chunk.similarity <= 1.0


async def test_another_tenant_retrieves_nothing(
    client: AsyncClient, fake_embedder: RankingFakeEmbedder
) -> None:
    """Tenant B searches tenant A's exact words and gets nothing.

    This is the EAP-15 of M3: isolation holds in the vector path too,
    because RLS filters the chunk rows before the ORDER BY ever runs.
    """
    a = await _seed_tenant()
    b = await _seed_tenant()

    await _ingest(
        client, await _token(client, a), "marina parking for residents", "marina parking for residents"
    )

    hits = await _retrieve(b["tenant_id"], query="marina parking")
    assert hits == []


async def test_the_owner_still_finds_it(client: AsyncClient, fake_embedder: RankingFakeEmbedder) -> None:
    """The partner to the test above: tenant A finds what B could not.

    Without this, an empty list for B proves only that retrieval is broken
    for everyone.
    """
    a = await _seed_tenant()
    await _seed_tenant()

    await _ingest(client, await _token(client, a), "Marina guide", "marina parking for residents")

    hits = await _retrieve(a["tenant_id"], query="marina parking")
    assert len(hits) == 1
    assert "marina" in hits[0].content
