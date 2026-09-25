"""
Seed a demo tenant so the platform can be exercised end to end.

Stands in for the tenant admin API (EAP-16, deferred). Idempotent - safe to re-run.

Uses the OWNER session factory: creating a tenant is inherently a cross-tenant operation,
so it cannot run under the restricted role that every request uses.
"""

import asyncio

from sqlalchemy import text

from eap.core.db import dispose_owner_engine, get_owner_session_factory
from eap.core.security import hash_password

SLUG = "acme"
EMAIL = "admin@acme.com"
PASSWORD = "demo-password-123"


async def main() -> None:
    async with get_owner_session_factory()() as s:
        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, 'Acme Corporation') "
                    "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id"
                ),
                {"slug": SLUG},
            )
        ).scalar_one()

        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash, role) "
                "VALUES (gen_random_uuid(), :tid, :email, :ph, 'owner') "
                "ON CONFLICT (tenant_id, email) DO UPDATE SET password_hash = EXCLUDED.password_hash"
            ),
            {"tid": tenant_id, "email": EMAIL, "ph": hash_password(PASSWORD)},
        )

        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'echo', 'Echo') "
                "ON CONFLICT (tenant_id, name) DO NOTHING"
            ),
            {"tid": tenant_id},
        )
        await s.commit()

    print(f"tenant : {SLUG}  ({tenant_id})")
    print(f"login  : {EMAIL} / {PASSWORD}")
    print("agents : echo")
    await dispose_owner_engine()


if __name__ == "__main__":
    asyncio.run(main())
