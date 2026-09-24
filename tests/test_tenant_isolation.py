"""
Cross-tenant isolation - the test M1 exists for.

It connects as eap_app, a NON-superuser role that owns no tables, so every RLS policy
applies. Run as the owner these assertions would all pass while proving nothing.

If someone deletes the RLS policy, this file must go red. That is its only job.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from eap.core.config import get_settings
from eap.core.db import get_session_factory
from eap.core.security import hash_password


def _app_role_url() -> str:
    """Same database, but connected as the restricted application role."""
    return str(get_settings().database_url).replace("//eap:eap@", "//eap_app:eap_app@")


@pytest.fixture
async def two_tenants() -> AsyncIterator[dict[str, str]]:
    """Seed two tenants, each with one user. Seeding runs as the owner, on purpose."""
    a_slug, b_slug = f"a-{uuid.uuid4().hex[:8]}", f"b-{uuid.uuid4().hex[:8]}"
    a_email, b_email = f"{uuid.uuid4().hex[:8]}@a.test", f"{uuid.uuid4().hex[:8]}@b.test"
    ids = {}

    async with get_session_factory()() as s:
        for key, slug, email in (("a", a_slug, a_email), ("b", b_slug, b_email)):
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
                {"tid": tid, "email": email, "ph": hash_password("x" * 12)},
            )
            ids[key] = str(tid)
        await s.commit()

    yield {"a": ids["a"], "b": ids["b"], "a_email": a_email, "b_email": b_email}


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(_app_role_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


async def test_app_role_is_not_superuser(app_engine: AsyncEngine) -> None:
    """Guard on the guard: if this role ever gains superuser, every test below is a lie."""
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).scalar_one()
    assert row is False, "app role can bypass RLS - isolation tests would prove nothing"


async def test_tenant_a_cannot_see_tenant_b_users(
    app_engine: AsyncEngine, two_tenants: dict[str, str]
) -> None:
    """The core claim: acting as tenant A, tenant B's rows simply do not exist."""
    async with app_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": two_tenants["a"]})

        # No WHERE tenant_id anywhere. RLS supplies it.
        emails = (await conn.execute(text("SELECT email FROM users"))).scalars().all()

    assert two_tenants["a_email"] in emails
    assert two_tenants["b_email"] not in emails


async def test_unset_tenant_sees_nothing(app_engine: AsyncEngine, two_tenants: dict[str, str]) -> None:
    """
    A request that forgets to set the tenant must see zero rows, not every row.

    current_setting(..., true) returns NULL when unset, so the policy comparison is NULL
    and nothing matches. Failing closed is the whole design.
    """
    async with app_engine.begin() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert count == 0


async def test_cannot_write_a_row_belonging_to_another_tenant(
    app_engine: AsyncEngine, two_tenants: dict[str, str]
) -> None:
    """
    Reads are only half of it. Acting as A, writing a row tagged B must be refused by the
    WITH CHECK half of the policy - otherwise A could plant data inside B.
    """
    with pytest.raises(DBAPIError):
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": two_tenants["a"]},
            )
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash) "
                    "VALUES (gen_random_uuid(), :tid, 'intruder@a.test', 'x')"
                ),
                {"tid": two_tenants["b"]},
            )
