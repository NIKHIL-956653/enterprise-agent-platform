"""Tenant context tests - proves the token -> session -> RLS chain works end to end."""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_session_factory
from eap.core.security import hash_password

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
async def logged_in(client: AsyncClient) -> AsyncIterator[dict[str, str]]:
    """Seed a tenant + user, log in, and hand back the token and the seeded ids."""
    slug = f"globex-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex[:8]}@example.com"

    async with get_session_factory()() as s:
        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, 'Globex') RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        user_id = (
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash) "
                    "VALUES (gen_random_uuid(), :tid, :email, :ph) RETURNING id"
                ),
                {"tid": tenant_id, "email": email, "ph": hash_password(PASSWORD)},
            )
        ).scalar_one()
        await s.commit()

    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": slug, "email": email, "password": PASSWORD},
    )
    assert r.status_code == 200, r.text

    yield {
        "token": r.json()["access_token"],
        "slug": slug,
        "email": email,
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
    }


async def test_me_returns_the_caller_identity(client: AsyncClient, logged_in: dict[str, str]) -> None:
    """The token alone identifies user AND tenant - no ids in the request at all."""
    r = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {logged_in['token']}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == logged_in["user_id"]
    assert body["tenant_id"] == logged_in["tenant_id"]
    assert body["tenant_slug"] == logged_in["slug"]
    assert body["email"] == logged_in["email"]
    assert body["role"] == "member"


@pytest.mark.parametrize(
    "header",
    [None, {"Authorization": "Bearer not-a-token"}, {"Authorization": "Basic abc"}],
    ids=["missing", "garbage-token", "wrong-scheme"],
)
async def test_me_rejects_bad_auth(client: AsyncClient, header: dict[str, str] | None) -> None:
    """No token, a forged token and the wrong scheme must all be 401 - never 403 or 500."""
    r = await client.get("/v1/auth/me", headers=header or {})
    assert r.status_code == 401


async def test_me_rejects_token_signed_with_another_secret(
    client: AsyncClient, logged_in: dict[str, str]
) -> None:
    """
    A token minted by someone who does not know our secret must not be accepted.

    This is the attack the whole design rests on: if a forged token were accepted, the
    attacker picks their own tid and RLS happily serves them that tenant's data.
    """
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": logged_in["user_id"], "tid": logged_in["tenant_id"], "role": "owner"},
        "a-completely-different-secret-that-is-long-enough-32",
        algorithm="HS256",
    )
    r = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
