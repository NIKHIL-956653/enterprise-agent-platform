"""
The demo seed (D1) and the two claims the demo rests on.

    1. One college cannot see another college's material.
    2. A student cannot see faculty-only material - and is never told it exists.

Both are already enforced by RLS and covered generically (EAP-15/35/40). These tests prove
them for the shape the demo actually uses: tenants, users, roles and clearances created by
scripts/seed_demo.py itself, reached only through the restricted role and real JWTs.

Every test seeds a SUFFIXED copy of the three colleges (engg-1a2b3c4d, ...). The suite
shares its database with the developer's demo stack, and a test that wrote documents into
the real `engg` would put test fixtures on the demo screen.
"""

import uuid

import pytest
from httpx import AsyncClient
from scripts import seed_demo
from scripts.seed_demo import ACME, CONFIDENTIAL, INTERNAL, RESTRICTED, college_tenants
from sqlalchemy import text

from eap.agents import runner
from eap.agents.answer import REFUSAL
from eap.agents.registry import registered_names
from eap.core.db import get_owner_session_factory, get_session_factory
from eap.rag import ingest
from eap.rag.retrieval import retrieve

# Bag-of-words over a fixed vocabulary, L2-normalised. Defined once, in test_retrieval.
from tests.test_retrieval import RankingFakeEmbedder

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "test-only-demo-password"

# Both texts share the query's vocabulary, so similarity cannot be what keeps either out.
# Whatever filters them is tenancy or clearance - the two things under test.
SYLLABUS = "Visitor parking at the marina is beside the pool and open to every resident."
EXAM_KEY = "Marina parking level two is reserved for board members and the beach gate code."
QUESTION = "Where is marina parking?"

# Words that would tell a student the restricted document exists. None may appear anywhere
# in what the student is shown.
LEAKY = ("denied", "forbidden", "restricted", "clearance", "classified", "permission", "not allowed")


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


class NoLLM:
    """Any model call fails the test: the refusals below must happen before a prompt exists."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        self.prompts.append(prompt)
        raise AssertionError("the model was called")


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> NoLLM:
    llm = NoLLM()
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: llm)
    return llm


async def _seed_colleges() -> tuple[str, dict[str, str]]:
    """A fresh suffixed copy of the three colleges. Returns (suffix, slug -> tenant id)."""
    suffix = f"-{uuid.uuid4().hex[:8]}"
    ids = await seed_demo.seed(college_tenants(suffix), password=PASSWORD)
    return suffix, {slug: str(tid) for slug, tid in ids.items()}


async def _token(client: AsyncClient, slug: str, email: str) -> str:
    r = await client.post("/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _ingest(client: AsyncClient, token: str, title: str, body: str, classification: int) -> None:
    r = await client.post(
        "/v1/documents",
        headers=_auth(token),
        json={"title": title, "body": body, "external_id": title, "classification": classification},
    )
    assert r.status_code == 201, r.text


async def _retrieve(tenant_id: str, clearance: int) -> list:
    """The request path: restricted role, transaction-local context, RLS does the scoping."""
    async with get_session_factory()() as s:
        await s.execute(
            text("SELECT set_config('app.tenant_id', :tid, true), set_config('app.clearance', :c, true)"),
            {"tid": tenant_id, "c": str(clearance)},
        )
        return await retrieve(session=s, query=QUESTION)


async def _ask(client: AsyncClient, token: str) -> dict:
    r = await client.post("/v1/agents/answer/run", json={"question": QUESTION}, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


# --- the seed itself ---------------------------------------------------------------------


async def _counts(slugs: list[str]) -> tuple[int, int, int]:
    async with get_owner_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT (SELECT count(*) FROM tenants WHERE slug = ANY(:s)),"
                    "       (SELECT count(*) FROM users u JOIN tenants t ON t.id = u.tenant_id"
                    "         WHERE t.slug = ANY(:s)),"
                    "       (SELECT count(*) FROM agents a JOIN tenants t ON t.id = a.tenant_id"
                    "         WHERE t.slug = ANY(:s))"
                ),
                {"s": slugs},
            )
        ).one()
    return tuple(row)


async def test_rerunning_the_seed_adds_no_rows() -> None:
    suffix, ids = await _seed_colleges()
    slugs = list(ids)
    first = await _counts(slugs)

    await seed_demo.seed(college_tenants(suffix), password=PASSWORD)
    await seed_demo.seed(college_tenants(suffix), password=PASSWORD)

    agents = len(registered_names())
    assert first == (3, 6, 3 * agents)
    assert await _counts(slugs) == first


async def test_the_seed_sets_the_roles_and_clearances_in_the_table() -> None:
    _, ids = await _seed_colleges()
    async with get_owner_session_factory()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT t.slug, u.email, u.role, u.clearance FROM users u "
                    "JOIN tenants t ON t.id = u.tenant_id WHERE t.slug = ANY(:s)"
                ),
                {"s": list(ids)},
            )
        ).all()
    got = {(email, role, clearance) for _, email, role, clearance in rows}
    assert got == {
        (f"{who}@{college}.edu", role, clearance)
        for college in ("engg", "mba", "bba")
        for who, role, clearance in (("admin", "admin", RESTRICTED), ("student", "member", INTERNAL))
    }


async def test_rerunning_repairs_a_stale_clearance() -> None:
    """The bug that started D1: a pre-0008 row stuck at clearance 0 that re-seeding never fixed."""
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    async with get_owner_session_factory()() as s:
        await s.execute(
            text("UPDATE users SET clearance = 0, role = 'member' WHERE tenant_id = :tid"),
            {"tid": ids[engg]},
        )
        await s.commit()

    await seed_demo.seed(college_tenants(suffix), password=PASSWORD)

    async with get_owner_session_factory()() as s:
        admin = (
            await s.execute(
                text("SELECT role, clearance FROM users WHERE tenant_id = :tid AND email = 'admin@engg.edu'"),
                {"tid": ids[engg]},
            )
        ).one()
    assert tuple(admin) == ("admin", RESTRICTED)


def test_acme_is_unchanged() -> None:
    # Same slug, same name, same single owner at clearance 3 - what the old script wrote.
    assert ACME.slug == "acme"
    assert ACME.name == "Acme Corporation"
    assert [(u.email, u.role, u.clearance) for u in ACME.users] == [("admin@acme.com", "owner", RESTRICTED)]
    assert seed_demo.DEMO_TENANTS[0] == ACME


def test_the_password_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's own .env must not leak into this test.
    monkeypatch.setattr(seed_demo, "dotenv_values", lambda _path: {})
    monkeypatch.setenv("DEMO_PASSWORD", "from-the-env")
    assert seed_demo.demo_password() == "from-the-env"
    monkeypatch.setenv("DEMO_PASSWORD", "")
    assert seed_demo.demo_password() == seed_demo.DEV_PASSWORD
    monkeypatch.delenv("DEMO_PASSWORD")
    assert seed_demo.demo_password() == seed_demo.DEV_PASSWORD


def test_the_password_falls_back_to_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.setattr(seed_demo, "dotenv_values", lambda _path: {"DEMO_PASSWORD": "from-dotenv"})
    assert seed_demo.demo_password() == "from-dotenv"
    monkeypatch.setenv("DEMO_PASSWORD", "from-the-env")
    assert seed_demo.demo_password() == "from-the-env"


async def test_every_seeded_user_can_log_in_and_sees_an_empty_list(client: AsyncClient) -> None:
    suffix, _ = await _seed_colleges()
    for spec in college_tenants(suffix):
        for user in spec.users:
            r = await client.get("/v1/documents", headers=_auth(await _token(client, spec.slug, user.email)))
            assert r.status_code == 200, r.text
            assert r.json() == {"items": [], "next_before": None}


# --- claim 1: one college cannot see another's material ----------------------------------


async def test_an_mba_user_retrieves_nothing_from_an_engg_document(
    client: AsyncClient, no_llm: NoLLM
) -> None:
    suffix, ids = await _seed_colleges()
    engg, mba = f"engg{suffix}", f"mba{suffix}"
    await _ingest(client, await _token(client, engg, "admin@engg.edu"), "Syllabus", SYLLABUS, INTERNAL)

    # Control: the document is there, and findable, from inside engg - so the empty result
    # below is isolation, not a query that would have missed anyway.
    assert [h.document_title for h in await _retrieve(ids[engg], INTERNAL)] == ["Syllabus"]

    # mba's ADMIN, the highest clearance mba has. Clearance never crosses a tenant boundary.
    assert await _retrieve(ids[mba], RESTRICTED) == []

    mba_admin = await _token(client, mba, "admin@mba.edu")
    listing = await client.get("/v1/documents", headers=_auth(mba_admin))
    assert listing.json()["items"] == []

    run = await _ask(client, mba_admin)
    assert run["output"] == {"answer": REFUSAL, "answered": False, "citations": []}
    assert no_llm.prompts == []


# --- claim 2: a student cannot see faculty-only material, and is not told it exists ------


async def test_a_student_cannot_see_a_restricted_document(client: AsyncClient, no_llm: NoLLM) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    admin = await _token(client, engg, "admin@engg.edu")
    await _ingest(client, admin, "Exam answer key", EXAM_KEY, RESTRICTED)

    # Control: faculty can find it.
    assert [h.document_title for h in await _retrieve(ids[engg], RESTRICTED)] == ["Exam answer key"]

    student = await _token(client, engg, "student@engg.edu")
    assert await _retrieve(ids[engg], INTERNAL) == []

    listing = await client.get("/v1/documents", headers=_auth(student))
    assert listing.status_code == 200
    assert listing.json() == {"items": [], "next_before": None}

    run = await _ask(client, student)
    assert run["status"] == "succeeded"
    assert run["output"] == {"answer": REFUSAL, "answered": False, "citations": []}
    assert no_llm.prompts == []


async def test_the_absence_is_indistinguishable_from_an_empty_tenant(
    client: AsyncClient, no_llm: NoLLM
) -> None:
    """
    The stronger form of the claim. A 403, an "access denied", or ANY difference between
    "there is a document you may not read" and "there is no document" tells the student
    the document exists. So: put a restricted document in engg, leave bba empty, and ask
    the same things as each college's student. Every response must match byte for byte,
    apart from ids and timings.
    """
    suffix, _ = await _seed_colleges()
    engg, bba = f"engg{suffix}", f"bba{suffix}"
    await _ingest(
        client, await _token(client, engg, "admin@engg.edu"), "Exam answer key", EXAM_KEY, RESTRICTED
    )
    await _ingest(
        client, await _token(client, engg, "admin@engg.edu"), "Marking scheme", EXAM_KEY, CONFIDENTIAL
    )

    seen: dict[str, dict] = {}
    for slug, email in ((engg, "student@engg.edu"), (bba, "student@bba.edu")):
        token = await _token(client, slug, email)
        listing = await client.get("/v1/documents", headers=_auth(token))
        run = await _ask(client, token)
        detail = await client.get(f"/v1/runs/{run['run_id']}", headers=_auth(token))
        audit = await client.get(f"/v1/runs/{run['run_id']}/audit", headers=_auth(token))
        assert {listing.status_code, detail.status_code, audit.status_code} == {200}
        seen[slug] = {
            "listing": listing.json(),
            "status": run["status"],
            "output": run["output"],
            "error": run["error"],
            "detail_output": detail.json()["output"],
            # What the student can read of their own run's trail: event types and payloads.
            "audit": [(e["event_type"], _without_timing(e["payload"])) for e in audit.json()["events"]],
        }

    assert seen[engg] == seen[bba]
    shown = str(seen[engg]).lower()
    assert not [w for w in LEAKY if w in shown], shown
    assert no_llm.prompts == []


def _without_timing(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k != "latency_ms"}


async def test_a_student_still_sees_internal_material(client: AsyncClient) -> None:
    """Guards the tests above against passing vacuously: students see clearance-1 documents."""
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    admin = await _token(client, engg, "admin@engg.edu")
    await _ingest(client, admin, "Syllabus", SYLLABUS, INTERNAL)
    await _ingest(client, admin, "Exam answer key", EXAM_KEY, RESTRICTED)

    student = await _token(client, engg, "student@engg.edu")
    titles = [d["title"] for d in (await client.get("/v1/documents", headers=_auth(student))).json()["items"]]
    assert titles == ["Syllabus"]
    assert [h.document_title for h in await _retrieve(ids[engg], INTERNAL)] == ["Syllabus"]
