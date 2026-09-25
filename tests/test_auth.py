"""Login endpoint tests - real database, real password hashing, no mocks."""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.jwt import decode_access_token
from eap.core.security import hash_password

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
async def seeded_tenant() -> AsyncIterator[dict[str, str]]:
    """Create one tenant with one user. Random slug/email so reruns never collide."""
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex[:8]}@example.com"

    async with get_owner_session_factory()() as s:
        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, 'Acme Corp') RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (gen_random_uuid(), :tid, :email, :ph)"
            ),
            {"tid": tenant_id, "email": email, "ph": hash_password(PASSWORD)},
        )
        await s.commit()

    yield {"slug": slug, "email": email, "tenant_id": str(tenant_id)}


async def test_login_returns_token_carrying_the_tenant(
    client: AsyncClient, seeded_tenant: dict[str, str]
) -> None:
    """The whole point of the token: it carries tid, so later requests never have to ask."""
    r = await client.post(
        "/v1/auth/login",
        json={
            "tenant_slug": seeded_tenant["slug"],
            "email": seeded_tenant["email"],
            "password": PASSWORD,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"

    claims = decode_access_token(body["access_token"])
    assert claims["tid"] == seeded_tenant["tenant_id"]
    assert claims["role"] == "member"


@pytest.mark.parametrize(
    "field,bad_value",
    [("password", "wrong-password"), ("tenant_slug", "no-such-tenant"), ("email", "nobody@example.com")],
)
async def test_login_rejects_bad_credentials_identically(
    client: AsyncClient, seeded_tenant: dict[str, str], field: str, bad_value: str
) -> None:
    """
    Wrong password, wrong tenant and unknown email must all answer the same way.

    A different message or status for "no such user" turns the login form into a directory
    of who your customers are - user enumeration.
    """
    payload = {
        "tenant_slug": seeded_tenant["slug"],
        "email": seeded_tenant["email"],
        "password": PASSWORD,
    }
    payload[field] = bad_value

    r = await client.post("/v1/auth/login", json=payload)
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid credentials"
