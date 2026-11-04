"""
EAP-67: within a tenant, a member sees only the runs they triggered; admins see the tenant.

Tenant isolation is RLS and is tested elsewhere (EAP-15, EAP-35). This file is about the
OTHER boundary, the one between two students of the same college: student B must not be
able to list, open, audit or vote on student A's run - and must not be told it exists. Every
"not yours" answer below is a 404, byte-identical to "never existed".
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "test-only-visibility-password"
ADMIN, A, B = "admin@x.edu", "a@x.edu", "b@x.edu"


async def _tenant() -> str:
    """One tenant: an admin and two members, with the echo agent enabled."""
    slug = f"vis-{uuid.uuid4().hex[:8]}"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()
        for email, role in ((ADMIN, "admin"), (A, "member"), (B, "member")):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, role) "
                    "VALUES (gen_random_uuid(), :tid, :e, :ph, :r)"
                ),
                {"tid": tid, "e": email, "ph": hash_password(PASSWORD), "r": role},
            )
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'echo', 'Echo')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return slug


async def _headers(client: AsyncClient, slug: str, email: str) -> dict[str, str]:
    r = await client.post("/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _echo(client: AsyncClient, h: dict[str, str], message: str) -> str:
    r = await client.post("/v1/agents/echo/run", json={"message": message}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


async def _ids(client: AsyncClient, h: dict[str, str]) -> set[str]:
    r = await client.get("/v1/runs", headers=h)
    assert r.status_code == 200, r.text
    return {i["run_id"] for i in r.json()["items"]}


async def test_a_member_sees_only_their_own_runs(client: AsyncClient) -> None:
    slug = await _tenant()
    ha, hb = await _headers(client, slug, A), await _headers(client, slug, B)
    run_a = await _echo(client, ha, "a's question")
    run_b = await _echo(client, hb, "b's question")

    assert await _ids(client, ha) == {run_a}
    assert await _ids(client, hb) == {run_b}

    # Each sees their own run in full ...
    assert (await client.get(f"/v1/runs/{run_a}", headers=ha)).status_code == 200
    assert (await client.get(f"/v1/runs/{run_a}/audit", headers=ha)).status_code == 200
    # ... and the other's as if it did not exist: 404 everywhere, never 403.
    for path in (f"/v1/runs/{run_a}", f"/v1/runs/{run_a}/audit"):
        r = await client.get(path, headers=hb)
        assert r.status_code == 404, (path, r.text)
        assert r.json() == {"detail": "run not found"}
    r = await client.post(f"/v1/runs/{run_a}/feedback", json={"verdict": "up"}, headers=hb)
    assert r.status_code == 404, r.text


async def test_the_404_matches_a_run_that_never_existed(client: AsyncClient) -> None:
    slug = await _tenant()
    ha, hb = await _headers(client, slug, A), await _headers(client, slug, B)
    run_a = await _echo(client, ha, "a's question")

    other = await client.get(f"/v1/runs/{run_a}", headers=hb)
    missing = await client.get(f"/v1/runs/{uuid.uuid4()}", headers=hb)

    assert (
        (other.status_code, other.json())
        == (missing.status_code, missing.json())
        == (404, {"detail": "run not found"})
    )


async def test_an_admin_sees_the_whole_tenant(client: AsyncClient) -> None:
    slug = await _tenant()
    ha, hb, hadmin = (await _headers(client, slug, e) for e in (A, B, ADMIN))
    run_a = await _echo(client, ha, "a's question")
    run_b = await _echo(client, hb, "b's question")

    assert await _ids(client, hadmin) == {run_a, run_b}
    assert (await client.get(f"/v1/runs/{run_b}", headers=hadmin)).status_code == 200
    assert (await client.get(f"/v1/runs/{run_b}/audit", headers=hadmin)).status_code == 200
    assert (
        await client.post(f"/v1/runs/{run_b}/feedback", json={"verdict": "down"}, headers=hadmin)
    ).status_code == 200


async def test_a_run_with_no_recorded_user_belongs_to_the_tenant(client: AsyncClient) -> None:
    """Runs from before triggered_by existed, or started by a script: admins only."""
    slug = await _tenant()
    ha, hadmin = await _headers(client, slug, A), await _headers(client, slug, ADMIN)
    run_a = await _echo(client, ha, "a's question")
    async with get_owner_session_factory()() as s:
        res = await s.execute(text("UPDATE agent_runs SET triggered_by = NULL WHERE id = :id"), {"id": run_a})
        assert res.rowcount == 1
        await s.commit()

    assert await _ids(client, ha) == set()
    assert (await client.get(f"/v1/runs/{run_a}", headers=ha)).status_code == 404
    assert await _ids(client, hadmin) == {run_a}
