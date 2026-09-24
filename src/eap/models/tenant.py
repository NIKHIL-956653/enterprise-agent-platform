"""
Tenant - an organisation using the platform. The root of the data model.

Every tenant-owned table carries tenant_id pointing here, and Postgres RLS filters on it
(see docs/ADR-002-tenant-isolation.md).
"""

import enum

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from eap.models.base import Base
from eap.models.meta import Timestamps, UUIDPk


class TenantStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Tenant(UUIDPk, Timestamps, Base):
    __tablename__ = "tenants"

    # Human-facing handle: URLs, logins, support tickets. Lowercase, stable, unique.
    # Users say "acme", never a UUID - but the UUID stays the key everything joins on,
    # so a tenant can be renamed without rewriting every foreign key.
    slug: Mapped[str] = mapped_column(String(63), unique=True, index=True)

    name: Mapped[str] = mapped_column(String(200))

    # native_enum=False stores a VARCHAR with a CHECK constraint instead of a Postgres
    # ENUM type - altering a CHECK is trivial, altering a PG enum is not.
    # values_callable stores the VALUES ("active") rather than the member NAMES
    # ("ACTIVE"); without it the CHECK allows ACTIVE/SUSPENDED while the column default
    # inserts "active", so every default insert violates its own constraint.
    status: Mapped[TenantStatus] = mapped_column(
        Enum(
            TenantStatus,
            native_enum=False,
            length=20,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        default=TenantStatus.ACTIVE,
        server_default=TenantStatus.ACTIVE.value,
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.slug}>"
