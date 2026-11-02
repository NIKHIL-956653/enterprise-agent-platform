"""
EAP-61 / ADR-006: the document write path must not reveal restricted external_ids.

Before migration 0012, (tenant_id, external_id) was unique across classifications. A student
posting an id that existed above their clearance conflicted with a row RLS hid from them, the
policy refused the update, and the API said 403 "classification exceeds your clearance" - while
a free id got 201. Existence only, but existence is exactly what the demo promises a student
is never told.

The first test is the requirement itself: identical responses for "exists but invisible" and
"does not exist". The others pin what the fix must not break: re-posting your own document
still replaces it, and re-posting at a new classification still moves a document you can see.
"""

import uuid

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.rag import ingest

# Bag-of-words over a fixed vocabulary, L2-normalised. Defined once, in test_retrieval.
from tests.test_retrieval import RankingFakeEmbedder

PASSWORD = "test-only-oracle-password"
EXAM_KEY = "Question one: the first law of thermodynamics. Question two: entropy of an isolated system."
NOTES = "Harmless study notes about the first law of thermodynamics and the meaning of entropy."

PUBLIC, INTERNAL, RESTRICTED = 0, 1, 3


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


async def _tenant() -> dict[str, str]:
    """One tenant, an admin at clearance 3 and a student at clearance 1."""
    slug = f"oracle-{uuid.uuid4().hex[:8]}"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()
        for email, role, clearance in (
            ("admin@x.edu", "admin", RESTRICTED),
            ("student@x.edu", "member", INTERNAL),
        ):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, role, clearance) "
                    "VALUES (gen_random_uuid(), :tid, :e, :ph, :r, :c)"
                ),
                {"tid": tid, "e": email, "ph": hash_password(PASSWORD), "r": role, "c": clearance},
            )
        await s.commit()
    return {"slug": slug, "tenant_id": str(tid)}


async def _token(client: AsyncClient, slug: str, email: str) -> str:
    r = await client.post("/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _post(client: AsyncClient, token: str, title: str, body: str, xid: str, cls: int) -> Response:
    return await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": title, "body": body, "external_id": xid, "classification": cls},
    )


async def _listing(client: AsyncClient, token: str) -> list[dict]:
    r = await client.get("/v1/documents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _rows(tenant_id: str) -> list[tuple[str, int, int]]:
    """(external_id, classification, chunk count) for every document in the tenant, as the owner."""
    async with get_owner_session_factory()() as s:
        rows = await s.execute(
            text(
                "SELECT d.external_id, d.classification, count(c.id) FROM documents d "
                "LEFT JOIN document_chunks c ON c.document_id = d.id "
                "WHERE d.tenant_id = :tid GROUP BY d.id ORDER BY d.external_id, d.classification"
            ),
            {"tid": tenant_id},
        )
        return [tuple(r) for r in rows]


def _comparable(r: Response, posted_id: str) -> dict:
    """Everything about a response except what is generated or what the caller chose."""
    body = r.json()
    assert body.pop("external_id") == posted_id
    assert uuid.UUID(body.pop("document_id"))  # generated, and a real uuid
    # date and x-request-id are generated per response by the server and the request-id
    # middleware; everything else, content-length included, must agree.
    headers = {k.lower(): v for k, v in r.headers.items() if k.lower() not in ("date", "x-request-id")}
    return {"status": r.status_code, "headers": headers, "body": body}


async def test_a_restricted_id_and_a_free_id_look_the_same_to_a_student(client: AsyncClient) -> None:
    t = await _tenant()
    admin = await _token(client, t["slug"], "admin@x.edu")
    assert (
        await _post(client, admin, "Exam answer key", EXAM_KEY, "exam-key", RESTRICTED)
    ).status_code == 201
    student = await _token(client, t["slug"], "student@x.edu")
    assert (
        await _listing(client, student) == []
    )  # the restricted document does not exist, as far as the student can tell

    taken = await _post(client, student, "My notes", NOTES, "exam-key", INTERNAL)
    free = await _post(client, student, "My notes", NOTES, "free-key", INTERNAL)

    # The requirement: nothing - status, headers, body - differs between "exists above my
    # clearance" and "does not exist". Both ids are the same length, so even content-length
    # has to agree.
    assert _comparable(taken, "exam-key") == _comparable(free, "free-key")
    assert taken.status_code == 201

    # The student sees exactly their two documents; the admin's copy is untouched.
    assert sorted((d["external_id"], d["classification"]) for d in await _listing(client, student)) == [
        ("exam-key", INTERNAL),
        ("free-key", INTERNAL),
    ]
    rows = await _rows(t["tenant_id"])
    assert [(x, c) for x, c, _ in rows] == [
        ("exam-key", INTERNAL),
        ("exam-key", RESTRICTED),
        ("free-key", INTERNAL),
    ]
    assert all(n > 0 for _, _, n in rows)


async def test_re_posting_your_own_document_still_replaces_it(client: AsyncClient) -> None:
    t = await _tenant()
    admin = await _token(client, t["slug"], "admin@x.edu")
    assert (
        await _post(client, admin, "Exam answer key", EXAM_KEY, "exam-key", RESTRICTED)
    ).status_code == 201
    restricted_before = [r for r in await _rows(t["tenant_id"]) if r[1] == RESTRICTED]
    student = await _token(client, t["slug"], "student@x.edu")

    first = await _post(client, student, "My notes", NOTES, "exam-key", INTERNAL)
    second = await _post(client, student, "My notes (edited)", NOTES, "exam-key", INTERNAL)

    assert first.status_code == second.status_code == 201
    assert first.json()["document_id"] == second.json()["document_id"]  # replaced, not duplicated
    listing = await _listing(client, student)
    assert [(d["external_id"], d["title"], d["chunk_count"]) for d in listing] == [
        ("exam-key", "My notes (edited)", second.json()["chunk_count"])
    ]
    # Still one student copy, chunks flat; the restricted copy exactly as it was.
    rows = await _rows(t["tenant_id"])
    assert [r for r in rows if r[1] == INTERNAL] == [("exam-key", INTERNAL, second.json()["chunk_count"])]
    assert [r for r in rows if r[1] == RESTRICTED] == restricted_before


async def test_re_posting_at_a_new_classification_moves_what_you_can_see(client: AsyncClient) -> None:
    t = await _tenant()
    admin = await _token(client, t["slug"], "admin@x.edu")
    student = await _token(client, t["slug"], "student@x.edu")
    assert (await _post(client, admin, "Syllabus", NOTES, "syllabus", INTERNAL)).status_code == 201
    assert [d["external_id"] for d in await _listing(client, student)] == ["syllabus"]

    # Reclassify by re-posting: the internal copy goes, the restricted one takes its place.
    moved = await _post(client, admin, "Syllabus", NOTES, "syllabus", RESTRICTED)

    assert moved.status_code == 201
    assert await _listing(client, student) == []
    assert [(x, c) for x, c, _ in await _rows(t["tenant_id"])] == [("syllabus", RESTRICTED)]


async def test_a_student_cannot_move_or_touch_a_copy_above_their_clearance(client: AsyncClient) -> None:
    t = await _tenant()
    admin = await _token(client, t["slug"], "admin@x.edu")
    student = await _token(client, t["slug"], "student@x.edu")
    assert (
        await _post(client, admin, "Exam answer key", EXAM_KEY, "exam-key", RESTRICTED)
    ).status_code == 201

    # Posting the same id at two levels the student CAN see consolidates those two only.
    assert (await _post(client, student, "My notes", NOTES, "exam-key", PUBLIC)).status_code == 201
    assert (await _post(client, student, "My notes", NOTES, "exam-key", INTERNAL)).status_code == 201

    assert [(x, c) for x, c, _ in await _rows(t["tenant_id"])] == [
        ("exam-key", INTERNAL),
        ("exam-key", RESTRICTED),
    ]


async def test_posting_above_your_own_clearance_is_still_refused(client: AsyncClient) -> None:
    # The one 403 that remains: the policy's WITH CHECK on the caller's own row.
    t = await _tenant()
    student = await _token(client, t["slug"], "student@x.edu")

    r = await _post(client, student, "Sneaky", NOTES, "sneaky", RESTRICTED)

    assert r.status_code == 403, r.text
    assert await _rows(t["tenant_id"]) == []
