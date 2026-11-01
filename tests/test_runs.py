"""
Run history endpoints.

Uses the echo agent so nothing here needs a model. The tenant-B test is the one that
matters: it proves history is RLS-scoped without a tenant filter anywhere in runs.py.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password

pytestmark = pytest.mark.usefixtures("temporal_worker")

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


async def _force_same_timestamp(table: str, ids: list[str]) -> None:
    """
    Put these rows on one created_at. The owner connection is a superuser in dev and CI,
    which is why this UPDATE sees the rows at all - FORCE RLS stops everyone else. The
    rowcount check makes a non-superuser owner fail here, loudly, not later and silently.
    """
    async with get_owner_session_factory()() as s:
        touched = 0
        for row_id in ids:
            res = await s.execute(
                text(f"UPDATE {table} SET created_at = TIMESTAMPTZ '2026-10-31 12:00:00+00' WHERE id = :id"),
                {"id": row_id},
            )
            touched += res.rowcount
        await s.commit()
    assert touched == len(ids), f"expected to update {len(ids)} rows, updated {touched}"


async def test_runs_sharing_a_timestamp_are_neither_skipped_nor_repeated(client) -> None:
    """
    EAP-37. The sort is (created_at, id) but the cursor used to carry created_at alone, so
    "older than T" lost the tied row on the far side of a page boundary. Page size 1 puts the
    boundary between the two tied rows, which is exactly where the old cursor failed.
    """
    h = await _headers(client)
    created = [await _echo(client, h, str(i)) for i in range(3)]
    await _force_same_timestamp("agent_runs", created[:2])

    seen: list[str] = []
    before = None
    for _ in range(10):  # bounded: a paging bug must fail, not loop forever
        page = (
            await client.get(
                "/v1/runs", params={"limit": 1, **({"before": before} if before else {})}, headers=h
            )
        ).json()
        seen += [i["run_id"] for i in page["items"]]
        before = page["next_before"]
        if before is None:
            break

    assert sorted(seen) == sorted(created)  # every run once, none twice
    assert len(seen) == len(set(seen))


async def test_a_bare_timestamp_cursor_still_means_strictly_older(client) -> None:
    h = await _headers(client)
    created = [await _echo(client, h, str(i)) for i in range(2)]
    newest = (await client.get("/v1/runs", params={"limit": 1}, headers=h)).json()["items"][0]

    page = (await client.get("/v1/runs", params={"before": newest["created_at"]}, headers=h)).json()

    assert [i["run_id"] for i in page["items"]] == [r for r in created if r != newest["run_id"]]


async def test_a_garbage_cursor_is_422(client) -> None:
    h = await _headers(client)
    assert (await client.get("/v1/runs", params={"before": "yesterday"}, headers=h)).status_code == 422
    assert (
        await client.get("/v1/runs", params={"before": "2026-01-01T00:00:00+00:00|not-a-uuid"}, headers=h)
    ).status_code == 422
