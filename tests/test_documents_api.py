"""
Document ingestion, end to end.

The embedder is faked - deterministic vectors, no network, no key needed. Everything else is
real: the route, the token, the tenant-scoped session, RLS, and Postgres with pgvector.
"""

import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.llm.base import LLMError
from eap.models.document import EMBEDDING_DIMENSIONS
from eap.rag import ingest

PASSWORD = "correct-horse-battery-staple"
BODY = (
    "Dubai Marina is a waterfront community on the western edge of the city. "
    "It is built around an artificial canal and is dense with high-rise towers.\n\n"
    "Units there are mostly apartments, from studios to four-bedroom penthouses. "
    "Service charges are higher than average because of the marina upkeep.\n\n"
    "The area is served by two metro stations and a tram loop. "
    "Parking is included with most units but visitor parking is limited."
)


class FakeEmbedder:
    """Deterministic and offline. Vector width matches the column exactly."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self._vector(t) for t in texts]

    async def embed_query(self, text_: str) -> list[float]:
        return self._vector(text_)

    def _vector(self, value: str) -> list[float]:
        seed = sum(value.encode()) % 97 + 1
        return [1.0 / seed] * EMBEDDING_DIMENSIONS


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    fake = FakeEmbedder()
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


async def _post(client: AsyncClient, token: str, **body):
    return await client.post("/v1/documents", headers={"Authorization": f"Bearer {token}"}, json=body)


async def test_ingest_stores_document_and_chunks(client: AsyncClient) -> None:
    token = await _token(client, await _seed_tenant())

    r = await _post(client, token, title="Dubai Marina", body=BODY, external_id="area:marina")

    assert r.status_code == 201, r.text
    assert r.json()["chunk_count"] >= 1
    assert r.json()["external_id"] == "area:marina"


async def test_embedding_is_stored_at_the_declared_width(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed)
    await _post(client, token, title="Marina", body=BODY, external_id="area:marina")

    async with get_owner_session_factory()() as s:
        width = (
            await s.execute(
                text("SELECT vector_dims(embedding) FROM document_chunks WHERE tenant_id = :tid LIMIT 1"),
                {"tid": seed["tenant_id"]},
            )
        ).scalar_one()
    # If this drifts from the column, every later INSERT fails opaquely mid-ingestion.
    assert width == EMBEDDING_DIMENSIONS


async def test_chunks_are_embedded_in_one_batched_call(
    client: AsyncClient, fake_embedder: FakeEmbedder
) -> None:
    token = await _token(client, await _seed_tenant())
    await _post(client, token, title="Marina", body=BODY)

    # One HTTP call per chunk is what makes ingesting a real document feel broken.
    assert len(fake_embedder.calls) == 1


async def test_reposting_the_same_external_id_replaces_it(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed)

    first = await _post(client, token, title="Marina", body=BODY, external_id="area:marina")
    second = await _post(client, token, title="Marina (updated)", body=BODY, external_id="area:marina")

    assert first.json()["document_id"] == second.json()["document_id"]

    async with get_owner_session_factory()() as s:
        docs = (
            await s.execute(
                text("SELECT count(*) FROM documents WHERE tenant_id = :tid"),
                {"tid": seed["tenant_id"]},
            )
        ).scalar_one()
        chunks = (
            await s.execute(
                text("SELECT count(*) FROM document_chunks WHERE tenant_id = :tid"),
                {"tid": seed["tenant_id"]},
            )
        ).scalar_one()

    # Two ingests, one document, one set of chunks. Appending instead would double the corpus
    # every re-ingest, with both copies matching every query.
    assert docs == 1
    assert chunks == second.json()["chunk_count"]


async def test_omitting_external_id_creates_separate_documents(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed)

    a = await _post(client, token, title="One", body=BODY)
    b = await _post(client, token, title="Two", body=BODY)

    assert a.json()["document_id"] != b.json()["document_id"]


async def test_a_body_with_no_content_is_422(client: AsyncClient) -> None:
    token = await _token(client, await _seed_tenant())
    r = await _post(client, token, title="Empty", body="   \n\n  \t ")
    assert r.status_code == 422


async def test_embedder_failure_is_503_not_500(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        dimensions = EMBEDDING_DIMENSIONS

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise LLMError("embedding call failed")

        async def embed_query(self, text_: str) -> list[float]:
            raise LLMError("embedding call failed")

    monkeypatch.setattr(ingest, "_default_embedder", lambda: Broken())
    token = await _token(client, await _seed_tenant())

    r = await _post(client, token, title="Marina", body=BODY)
    # The request was valid and retrying later is right, so 503. And the provider's own error
    # text must never reach the caller.
    assert r.status_code == 503
    assert "Gemini" not in r.text and "google" not in r.text.lower()


async def test_nothing_is_written_when_embedding_fails(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        dimensions = EMBEDDING_DIMENSIONS

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise LLMError("embedding call failed")

        async def embed_query(self, text_: str) -> list[float]:
            raise LLMError("embedding call failed")

    monkeypatch.setattr(ingest, "_default_embedder", lambda: Broken())
    seed = await _seed_tenant()
    token = await _token(client, seed)

    await _post(client, token, title="Marina", body=BODY)

    async with get_owner_session_factory()() as s:
        docs = (
            await s.execute(
                text("SELECT count(*) FROM documents WHERE tenant_id = :tid"),
                {"tid": seed["tenant_id"]},
            )
        ).scalar_one()
    # A document row with no chunks under it looks ingested and is permanently unsearchable.
    assert docs == 0


async def test_another_tenant_cannot_see_the_document(client: AsyncClient) -> None:
    owner = await _seed_tenant()
    stranger = await _seed_tenant()
    owner_token = await _token(client, owner)
    stranger_token = await _token(client, stranger)

    await _post(client, owner_token, title="Marina", body=BODY, external_id="area:marina")
    # Same external_id, different tenant: must create its own document, not collide.
    r = await _post(client, stranger_token, title="Marina", body=BODY, external_id="area:marina")

    assert r.status_code == 201

    async with get_owner_session_factory()() as s:
        for tid in (owner["tenant_id"], stranger["tenant_id"]):
            count = (
                await s.execute(
                    text("SELECT count(*) FROM documents WHERE tenant_id = :tid"),
                    {"tid": tid},
                )
            ).scalar_one()
            assert count == 1


# --- D4: a readable external_id when the caller supplies none ----------------------------


async def test_an_omitted_external_id_is_a_slug_of_the_title(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed)

    r = await _post(client, token, title="Marina Handbook (2026)!", body=BODY)

    assert r.status_code == 201, r.text
    xid = r.json()["external_id"]
    assert re.fullmatch(r"marina-handbook-2026-[0-9a-f]{8}", xid), xid
    # The same id is what the list - and the answer agent's citations - will show.
    listing = await client.get("/v1/documents", headers={"Authorization": f"Bearer {token}"})
    assert [d["external_id"] for d in listing.json()["items"]] == [xid]


async def test_the_same_title_twice_without_an_id_is_two_documents(client: AsyncClient) -> None:
    # "Omitted means a new document every time" survives the readable id.
    seed = await _seed_tenant()
    token = await _token(client, seed)

    a = (await _post(client, token, title="Syllabus", body=BODY)).json()
    b = (await _post(client, token, title="Syllabus", body=BODY)).json()

    assert a["document_id"] != b["document_id"]
    assert a["external_id"] != b["external_id"]
    assert a["external_id"].startswith("syllabus-") and b["external_id"].startswith("syllabus-")


async def test_a_suffix_collision_draws_again_rather_than_replacing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the random suffix to repeat once. The second post must get a fresh id, never
    # the first document's - replacing an unrelated document would be silent data loss.
    draws = iter(["aaaaaaaa", "aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(ingest, "_suffix", lambda: next(draws))
    seed = await _seed_tenant()
    token = await _token(client, seed)

    a = (await _post(client, token, title="Syllabus", body=BODY)).json()
    b = (await _post(client, token, title="Syllabus", body=BODY)).json()

    assert a["external_id"] == "syllabus-aaaaaaaa"
    assert b["external_id"] == "syllabus-bbbbbbbb"
    assert a["document_id"] != b["document_id"]


async def test_an_explicit_external_id_is_used_verbatim(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed)

    r = await _post(client, token, title="Marina", body=BODY, external_id="Thermo Notes (v2).pdf")

    assert r.json()["external_id"] == "Thermo Notes (v2).pdf"


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Marina Handbook", "marina-handbook"),
        ("  Thermodynamics — Unit 1 / Part A  ", "thermodynamics-unit-1-part-a"),
        # Letters in any script survive; only spaces and punctuation become '-'. A title in
        # Arabic must not collapse to "document".
        ("دليل المرسى", "دليل-المرسى"),
        ("!!! ???", "document"),
        ("x" * 100, "x" * ingest.SLUG_MAX),
        ("Ends with punctuation...", "ends-with-punctuation"),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert ingest.slugify(title) == slug
