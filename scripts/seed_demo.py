"""
Seed the demo tenants so the platform can be exercised end to end.

Stands in for the tenant admin API (EAP-16, deferred). Idempotent - safe to re-run: every
row is an upsert on its natural key, so a second run updates in place and adds nothing.

    python scripts/seed_demo.py

Four tenants:

    acme   Acme Corporation      admin@acme.com    owner  clearance 3
    engg   Engineering College   admin@engg.edu    admin  clearance 3
                                 student@engg.edu  member clearance 1
    mba    MBA College           admin@mba.edu     admin  clearance 3
                                 student@mba.edu   member clearance 1
    bba    BBA College           admin@bba.edu     admin  clearance 3
                                 student@bba.edu   member clearance 1

The three colleges are the isolation demo: one college cannot see another's material, and
a student (clearance 1) cannot see faculty-only material (classification 2-3) - nor learn
that it exists. Acme is the original single demo tenant and stays exactly as it was.

Every user's password is DEMO_PASSWORD (environment first, then .env), defaulting to the dev-only
value below. Nothing here is a real credential; set DEMO_PASSWORD before seeding anything
reachable from outside your machine.

Uses the OWNER session factory: creating a tenant is inherently a cross-tenant operation,
so it cannot run under the restricted role that every request uses.
"""

import asyncio
import os
from dataclasses import dataclass
from uuid import UUID

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.agents import registry
from eap.core.db import dispose_owner_engine, get_owner_session_factory
from eap.core.security import hash_password

DEV_PASSWORD = "demo-password-123"

# Clearance levels, as enforced by the RLS policy from migration 0008.
PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED = 0, 1, 2, 3


@dataclass(frozen=True)
class UserSpec:
    email: str
    role: str  # owner | admin | member - see eap.models.user.UserRole
    clearance: int


@dataclass(frozen=True)
class TenantSpec:
    slug: str
    name: str
    users: tuple[UserSpec, ...]


def _college(slug: str, name: str, suffix: str = "") -> TenantSpec:
    return TenantSpec(
        slug=f"{slug}{suffix}",
        name=name,
        users=(
            UserSpec(f"admin@{slug}.edu", "admin", RESTRICTED),
            UserSpec(f"student@{slug}.edu", "member", INTERNAL),
        ),
    )


def college_tenants(suffix: str = "") -> tuple[TenantSpec, ...]:
    """
    The three demo colleges. `suffix` exists for the tests: they seed suffixed copies so
    they can write documents into them without touching the real demo tenants, which share
    the database with the test suite on a developer machine.
    """
    return (
        _college("engg", "Engineering College", suffix),
        _college("mba", "MBA College", suffix),
        _college("bba", "BBA College", suffix),
    )


ACME = TenantSpec(
    slug="acme",
    name="Acme Corporation",
    users=(UserSpec("admin@acme.com", "owner", RESTRICTED),),
)

DEMO_TENANTS: tuple[TenantSpec, ...] = (ACME, *college_tenants())


def demo_password() -> str:
    # Same lookup order as the per-tenant secrets (eap.llm.secrets): process environment,
    # then .env, then the dev default. `or` rather than a .get default, so an exported but
    # empty DEMO_PASSWORD falls through instead of seeding users with an empty password.
    return os.environ.get("DEMO_PASSWORD") or dotenv_values(".env").get("DEMO_PASSWORD") or DEV_PASSWORD


async def _upsert_tenant(s: AsyncSession, spec: TenantSpec, password_hash: str, agents: list[str]) -> UUID:
    tenant_id = (
        await s.execute(
            text(
                "INSERT INTO tenants (id, slug, name) "
                "VALUES (gen_random_uuid(), :slug, :name) "
                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id"
            ),
            {"slug": spec.slug, "name": spec.name},
        )
    ).scalar_one()

    for user in spec.users:
        # role and clearance are upserted, not just the password. A row created before
        # migration 0008 got clearance's server default of 0, and an upsert that only
        # refreshed the password left it there forever - an owner who could not upload a
        # classification-1 document. The spec is the source of truth; re-running enforces it.
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash, role, clearance) "
                "VALUES (gen_random_uuid(), :tid, :email, :ph, :role, :clr) "
                "ON CONFLICT (tenant_id, email) DO UPDATE SET "
                "  password_hash = EXCLUDED.password_hash,"
                "  role = EXCLUDED.role,"
                "  clearance = EXCLUDED.clearance"
            ),
            {
                "tid": tenant_id,
                "email": user.email,
                "ph": password_hash,
                "role": user.role,
                "clr": user.clearance,
            },
        )

    for name in agents:
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, :name, :display) "
                "ON CONFLICT (tenant_id, name) DO NOTHING"
            ),
            {"tid": tenant_id, "name": name, "display": registry.get_agent(name).display_name},
        )
    return tenant_id


async def seed(
    tenants: tuple[TenantSpec, ...] = DEMO_TENANTS, password: str | None = None
) -> dict[str, UUID]:
    """Upsert every tenant, its users and every registered agent. Returns slug -> tenant id."""
    # One hash for every user: argon2id is deliberately slow, and the salt makes the same
    # hash safe to reuse - it is still a valid argon2 hash of the same password.
    password_hash = hash_password(password or demo_password())
    # Enabled agents come from the registry, not a list kept here (EAP-39).
    agents = registry.registered_names()
    ids: dict[str, UUID] = {}
    async with get_owner_session_factory()() as s:
        for spec in tenants:
            ids[spec.slug] = await _upsert_tenant(s, spec, password_hash, agents)
        await s.commit()
    return ids


async def main() -> None:
    ids = await seed()
    await dispose_owner_engine()

    using_default = demo_password() == DEV_PASSWORD
    print(f"password : {'dev default (set DEMO_PASSWORD to change)' if using_default else 'DEMO_PASSWORD'}")
    print(f"agents   : {', '.join(registry.registered_names())}")
    print()
    print(f"{'tenant':<8} {'name':<22} {'login':<20} {'role':<7} clearance")
    for spec in DEMO_TENANTS:
        for i, user in enumerate(spec.users):
            slug, name = (spec.slug, spec.name) if i == 0 else ("", "")
            print(f"{slug:<8} {name:<22} {user.email:<20} {user.role:<7} {user.clearance}")
    print()
    print("tenant ids: " + ", ".join(f"{slug}={tid}" for slug, tid in ids.items()))


if __name__ == "__main__":
    asyncio.run(main())
