"""
Run history endpoints.

Uses the echo agent so nothing here needs a model. The tenant-B test is the one that
matters: it proves history is RLS-scoped without a tenant filter anywhere in runs.py.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password

PASSWORD = "correct-horse-battery-staple"


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
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'echo', 'Echo')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return {"slug": slug, "email": email}


async def _headers(client: AsyncClient) -> dict[str, str]:
    seed = await _seed_tenant()
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed["email"], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _echo(client: AsyncClient, headers: dict[str, str], message: str) -> str:
    r = await client.post("/v1/agents/echo/run", json={"message": message}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


async def test_list_is_newest_first_and_omits_payloads(client) -> None:
    h = await _headers(client)
    first = await _echo(client, h, "one")
    second = await _echo(client, h, "two")

    r = await client.get("/v1/runs", headers=h)

    assert r.status_code == 200, r.text
    ids = [item["run_id"] for item in r.json()["items"]]
    assert ids == [second, first]
    assert "input" not in r.json()["items"][0]
    assert "output" not in r.json()["items"][0]
    assert r.json()["next_before"] is None


async def test_detail_includes_payloads(client) -> None:
    h = await _headers(client)
    run_id = await _echo(client, h, "hello")

    r = await client.get(f"/v1/runs/{run_id}", headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["input"] == {"message": "hello"}
    assert r.json()["output"]["message"] == "hello"
    assert r.json()["status"] == "succeeded"


async def test_keyset_pagination_walks_all_runs_exactly_once(client) -> None:
    h = await _headers(client)
    created = [await _echo(client, h, str(i)) for i in range(3)]

    page1 = (await client.get("/v1/runs", params={"limit": 2}, headers=h)).json()
    assert len(page1["items"]) == 2
    assert page1["next_before"] is not None

    page2 = (
        await client.get("/v1/runs", params={"limit": 2, "before": page1["next_before"]}, headers=h)
    ).json()
    assert len(page2["items"]) == 1
    assert page2["next_before"] is None

    seen = [i["run_id"] for i in page1["items"]] + [i["run_id"] for i in page2["items"]]
    assert sorted(seen) == sorted(created)  # every run once, none twice


async def test_filters_by_agent_and_status(client) -> None:
    h = await _headers(client)
    await _echo(client, h, "x")

    assert len((await client.get("/v1/runs", params={"agent": "echo"}, headers=h)).json()["items"]) == 1
    assert len((await client.get("/v1/runs", params={"agent": "nope"}, headers=h)).json()["items"]) == 0
    assert len((await client.get("/v1/runs", params={"status": "failed"}, headers=h)).json()["items"]) == 0


async def test_naive_before_is_rejected(client) -> None:
    h = await _headers(client)
    r = await client.get("/v1/runs", params={"before": "2026-01-01T00:00:00"}, headers=h)
    assert r.status_code == 422


async def test_other_tenant_sees_nothing(client) -> None:
    h_a = await _headers(client)
    run_a = await _echo(client, h_a, "secret")
    h_b = await _headers(client)

    listing = await client.get("/v1/runs", headers=h_b)
    detail = await client.get(f"/v1/runs/{run_a}", headers=h_b)

    assert listing.json()["items"] == []
    assert detail.status_code == 404
