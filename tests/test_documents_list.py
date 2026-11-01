"""
GET /v1/documents (EAP-52): the browse half of the documents API.

Same experiment as the runs listing, with one addition that matters: clearance. The listing
never shows a document the caller is not cleared for - not as a greyed-out row, not as a
redacted title. RLS filters it, so to a low-clearance user the restricted document does not
exist, which is the only rendering of "restricted" that cannot leak.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.rag import ingest
from tests.test_retrieval import RankingFakeEmbedder

PASSWORD = "correct-horse-battery-staple"
BODY = "Resident parking is in the marina basement on level two. Visitor parking is by the pool."


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


async def _seed_tenant() -> dict[str, str]:
    """One tenant, two users: clearance 0 and clearance 3."""
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
        for email, clearance in ((low, 0), (high, 3)):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, clearance) "
                    "VALUES (gen_random_uuid(), :tid, :email, :ph, :clr)"
                ),
                {"tid": tid, "email": email, "ph": hash_password(PASSWORD), "clr": clearance},
            )
        await s.commit()
    return {"slug": slug, "low": low, "high": high}


async def _token(client: AsyncClient, seed: dict[str, str], who: str) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed[who], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _ingest(client: AsyncClient, token: str, title: str, classification: int = 0) -> None:
    r = await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": title, "body": BODY, "external_id": f"doc:{title}", "classification": classification},
    )
    assert r.status_code == 201, r.text


async def _list(client: AsyncClient, token: str, **params) -> dict:
    r = await client.get("/v1/documents", headers={"Authorization": f"Bearer {token}"}, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_newest_first_with_chunk_counts_and_no_bodies(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed, "high")
    for title in ("first", "second", "third"):
        await _ingest(client, token, title)

    page = await _list(client, token)

    assert [d["title"] for d in page["items"]] == ["third", "second", "first"]
    assert page["next_before"] is None
    for d in page["items"]:
        assert d["chunk_count"] >= 1  # the join counted real chunks
        assert "body" not in d  # a list is a browse surface, not a bulk export


async def test_keyset_pagination_walks_all_documents_exactly_once(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed, "high")
    for i in range(5):
        await _ingest(client, token, f"doc-{i}")

    seen: list[str] = []
    before = None
    for _ in range(10):  # bounded: a paging bug must fail, not loop forever
        page = await _list(client, token, limit=2, **({"before": before} if before else {}))
        seen += [d["external_id"] for d in page["items"]]
        before = page["next_before"]
        if before is None:
            break

    assert sorted(seen) == sorted(f"doc:doc-{i}" for i in range(5))
    assert len(seen) == len(set(seen))


async def test_other_tenant_sees_nothing(client: AsyncClient) -> None:
    seed_a = await _seed_tenant()
    seed_b = await _seed_tenant()
    await _ingest(client, await _token(client, seed_a, "high"), "private-to-a")

    page = await _list(client, await _token(client, seed_b, "high"))

    assert page["items"] == []  # not an error, not a filter flag: the rows do not exist here


async def test_clearance_filters_the_listing(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    high = await _token(client, seed, "high")
    await _ingest(client, high, "everyone", classification=0)
    await _ingest(client, high, "board-only", classification=3)

    high_titles = [d["title"] for d in (await _list(client, high))["items"]]
    low_titles = [d["title"] for d in (await _list(client, await _token(client, seed, "low")))["items"]]

    assert sorted(high_titles) == ["board-only", "everyone"]
    assert low_titles == ["everyone"]  # the restricted row is absent, not redacted


async def test_naive_before_is_422(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    token = await _token(client, seed, "high")
    r = await client.get(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        params={"before": "2026-10-22T10:00:00"},  # no timezone
    )
    assert r.status_code == 422


async def test_documents_sharing_a_timestamp_are_neither_skipped_nor_repeated(client: AsyncClient) -> None:
    """EAP-37, same shape as the runs test: tied rows straddling a page boundary of 1."""
    from tests.test_runs import _force_same_timestamp

    seed = await _seed_tenant()
    token = await _token(client, seed, "high")
    for i in range(4):
        await _ingest(client, token, f"doc-{i}")
    first_page = await _list(client, token, limit=4)
    await _force_same_timestamp("documents", [d["document_id"] for d in first_page["items"][:2]])

    seen: list[str] = []
    before = None
    for _ in range(10):
        page = await _list(client, token, limit=1, **({"before": before} if before else {}))
        seen += [d["external_id"] for d in page["items"]]
        before = page["next_before"]
        if before is None:
            break

    assert sorted(seen) == sorted(f"doc:doc-{i}" for i in range(4))
    assert len(seen) == len(set(seen))
