"""
Answer feedback (EAP-63): POST /v1/runs/{id}/feedback.

Two things matter here and both are about the database, not the handler. A run from
another tenant must read as MISSING (404) - a 403 would tell the caller the run exists -
and that must hold with no tenant check in application code. And a person gets one vote per
run: a second POST rewrites the first row, it never adds one.

The echo agent produces the runs, so nothing here needs a model.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap.core.db import get_owner_session_factory, get_session_factory
from eap.core.security import hash_password

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "correct-horse-battery-staple"


async def _seed_tenant(users: int = 1) -> dict:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    emails = [f"{uuid.uuid4().hex[:8]}@example.com" for _ in range(users)]
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
        for email in emails:
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
                "VALUES (gen_random_uuid(), :tid, 'echo', 'Echo')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return {"slug": slug, "emails": emails, "tenant_id": str(tid)}


async def _headers(client: AsyncClient, seed: dict, who: int = 0) -> dict[str, str]:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed["emails"][who], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _echo(client: AsyncClient, headers: dict[str, str]) -> str:
    r = await client.post("/v1/agents/echo/run", json={"message": "hi"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


async def _rows(tenant_id: str) -> list:
    """Every feedback row of one tenant, read as the owner so RLS cannot hide a stray one."""
    async with get_owner_session_factory()() as s:
        return (
            await s.execute(
                text("SELECT run_id, user_id, verdict, comment FROM feedback WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).all()


async def _all_rows_for_run(run_id: str) -> list:
    async with get_owner_session_factory()() as s:
        return (
            await s.execute(text("SELECT tenant_id FROM feedback WHERE run_id = :rid"), {"rid": run_id})
        ).all()


# --- the happy path -----------------------------------------------------------------------


async def test_thumbs_up_on_your_own_run(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    h = await _headers(client, seed)
    run_id = await _echo(client, h)

    r = await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "up"}, headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["run_id"] == run_id
    assert r.json()["verdict"] == "up"
    assert r.json()["comment"] is None
    rows = await _rows(seed["tenant_id"])
    assert [(str(x.run_id), x.verdict, x.comment) for x in rows] == [(run_id, "up", None)]


async def test_one_vote_per_person_per_run_is_upserted_on_change(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    h = await _headers(client, seed)
    run_id = await _echo(client, h)

    assert (
        await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "up"}, headers=h)
    ).status_code == 200
    r = await client.post(
        f"/v1/runs/{run_id}/feedback",
        json={"verdict": "down", "comment": "  cited the wrong unit  "},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {**r.json(), "verdict": "down", "comment": "cited the wrong unit"}

    rows = await _rows(seed["tenant_id"])
    assert len(rows) == 1, "a changed mind rewrites the row, it does not add one"
    assert (rows[0].verdict, rows[0].comment) == ("down", "cited the wrong unit")

    # Back to up with no comment clears the comment - the row says what they say NOW.
    assert (
        await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "up"}, headers=h)
    ).status_code == 200
    rows = await _rows(seed["tenant_id"])
    assert len(rows) == 1
    assert (rows[0].verdict, rows[0].comment) == ("up", None)


async def test_two_people_can_each_vote_once_on_the_same_run(client: AsyncClient) -> None:
    seed = await _seed_tenant(users=2)
    # Since EAP-67 a member sees - and can vote on - only their own runs, so the second
    # voter here is an admin: the two people who CAN both see one run. The property under
    # test is unchanged: one vote per person, re-voting replaces.
    async with get_owner_session_factory()() as s:
        await s.execute(
            text("UPDATE users SET role = 'admin' WHERE tenant_id = :tid AND email = :email"),
            {"tid": seed["tenant_id"], "email": seed["emails"][1]},
        )
        await s.commit()
    h1, h2 = await _headers(client, seed, 0), await _headers(client, seed, 1)
    run_id = await _echo(client, h1)

    assert (
        await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "up"}, headers=h1)
    ).status_code == 200
    assert (
        await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "down"}, headers=h2)
    ).status_code == 200
    assert (
        await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "down"}, headers=h1)
    ).status_code == 200

    rows = await _rows(seed["tenant_id"])
    assert len(rows) == 2
    assert sorted(x.verdict for x in rows) == ["down", "down"]
    assert len({x.user_id for x in rows}) == 2


# --- the shape ------------------------------------------------------------------------------


@pytest.mark.parametrize("body", [{"verdict": "sideways"}, {}, {"verdict": "up", "comment": "x" * 501}])
async def test_a_bad_body_is_422(client: AsyncClient, body: dict) -> None:
    seed = await _seed_tenant()
    h = await _headers(client, seed)
    run_id = await _echo(client, h)
    assert (await client.post(f"/v1/runs/{run_id}/feedback", json=body, headers=h)).status_code == 422
    assert await _rows(seed["tenant_id"]) == []


async def test_the_verdict_check_holds_against_raw_sql() -> None:
    """The API's Literal is one guard; the CHECK is the one every writer meets."""
    seed = await _seed_tenant()
    async with get_owner_session_factory()() as s:
        uid = (
            await s.execute(text("SELECT id FROM users WHERE tenant_id = :tid"), {"tid": seed["tenant_id"]})
        ).scalar_one()
        aid = (
            await s.execute(text("SELECT id FROM agents WHERE tenant_id = :tid"), {"tid": seed["tenant_id"]})
        ).scalar_one()
        rid = (
            await s.execute(
                text(
                    "INSERT INTO agent_runs (id, tenant_id, agent_id, input) "
                    "VALUES (gen_random_uuid(), :tid, :aid, '{}') RETURNING id"
                ),
                {"tid": seed["tenant_id"], "aid": aid},
            )
        ).scalar_one()
        await s.commit()
    async with get_owner_session_factory()() as s:
        with pytest.raises(IntegrityError, match="ck_feedback_verdict"):
            await s.execute(
                text(
                    "INSERT INTO feedback (id, tenant_id, run_id, user_id, verdict) "
                    "VALUES (gen_random_uuid(), :tid, :rid, :uid, 'sideways')"
                ),
                {"tid": seed["tenant_id"], "rid": rid, "uid": uid},
            )


async def test_an_unknown_run_is_404(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    h = await _headers(client, seed)
    r = await client.post(f"/v1/runs/{uuid.uuid4()}/feedback", json={"verdict": "up"}, headers=h)
    assert r.status_code == 404


async def test_feedback_needs_a_token(client: AsyncClient) -> None:
    r = await client.post(f"/v1/runs/{uuid.uuid4()}/feedback", json={"verdict": "up"})
    assert r.status_code == 401


# --- the one that matters: tenancy --------------------------------------------------------


async def test_another_tenants_run_is_404_not_403_and_leaves_no_row(client: AsyncClient) -> None:
    a, b = await _seed_tenant(), await _seed_tenant()
    ha, hb = await _headers(client, a), await _headers(client, b)
    run_id = await _echo(client, ha)

    r = await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "down"}, headers=hb)

    # Identical to a run that never existed: status AND body.
    missing = await client.post(f"/v1/runs/{uuid.uuid4()}/feedback", json={"verdict": "down"}, headers=hb)
    assert r.status_code == 404
    assert r.status_code == missing.status_code
    assert r.json() == missing.json()
    assert await _all_rows_for_run(run_id) == []
    assert await _rows(b["tenant_id"]) == []

    # The owner can still vote on their own run afterwards - nothing was consumed.
    assert (
        await client.post(f"/v1/runs/{run_id}/feedback", json={"verdict": "up"}, headers=ha)
    ).status_code == 200


async def test_the_database_refuses_a_cross_tenant_vote_even_without_the_handler(client: AsyncClient) -> None:
    """
    Bypass the endpoint: as eap_app in tenant B's context, insert feedback on A's run
    directly. RLS pins tenant_id to B; the composite key (tenant_id, run_id) finds no
    such run in B; the INSERT fails. The 404 above is the handler being polite - this is
    the property underneath it.
    """
    a, b = await _seed_tenant(), await _seed_tenant()
    run_id = await _echo(client, await _headers(client, a))
    async with get_owner_session_factory()() as s:
        b_user = (
            await s.execute(text("SELECT id FROM users WHERE tenant_id = :tid"), {"tid": b["tenant_id"]})
        ).scalar_one()

    async with get_session_factory()() as s:  # the restricted role, as every request uses
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": b["tenant_id"]})
        with pytest.raises(IntegrityError):
            await s.execute(
                text(
                    "INSERT INTO feedback (id, tenant_id, run_id, user_id, verdict) "
                    "VALUES (gen_random_uuid(), :tid, :rid, :uid, 'up')"
                ),
                {"tid": b["tenant_id"], "rid": run_id, "uid": b_user},
            )
    assert await _all_rows_for_run(run_id) == []


async def test_feedback_has_rls_with_force_like_every_tenant_table() -> None:
    async with get_owner_session_factory()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity,"
                    "       (SELECT count(*) FROM pg_policies p WHERE p.tablename = 'feedback'"
                    "          AND p.policyname = 'tenant_isolation')"
                    "  FROM pg_class c WHERE c.relname = 'feedback'"
                )
            )
        ).one()
    assert tuple(row) == (True, True, 1)


async def test_a_tenant_reads_only_its_own_votes(client: AsyncClient) -> None:
    a, b = await _seed_tenant(), await _seed_tenant()
    ha, hb = await _headers(client, a), await _headers(client, b)
    await client.post(f"/v1/runs/{await _echo(client, ha)}/feedback", json={"verdict": "up"}, headers=ha)
    await client.post(f"/v1/runs/{await _echo(client, hb)}/feedback", json={"verdict": "down"}, headers=hb)

    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": a["tenant_id"]})
        seen = (await s.execute(text("SELECT verdict FROM feedback"))).scalars().all()
    assert seen == ["up"]
