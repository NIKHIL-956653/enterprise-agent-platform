"""
Tenant - an organisation using the platform. The root of the data model.

Every tenant-owned table carries tenant_id pointing here, and Postgres RLS filters on it
(see docs/ADR-002-tenant-isolation.md).
"""

import enum

from sqlalchemy import CheckConstraint, Enum, Integer, String, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from eap.i18n import Language
from eap.models.base import Base
from eap.models.meta import Timestamps, UUIDPk, language_type

# Enough for a few hundred RAG answers a day (an answer run is a few thousand tokens), and
# small enough that a runaway loop on a new tenant is a bad afternoon, not a bad month.
# Migration 0014 writes the same number as the column's server default.
DEFAULT_DAILY_TOKEN_BUDGET = 500_000


class TenantStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Tenant(UUIDPk, Timestamps, Base):
    __tablename__ = "tenants"

    __table_args__ = (CheckConstraint("daily_token_budget >= 0", name="daily_token_budget"),)

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

    # What the tenant speaks when nothing more specific is known - for example a user who
    # never picked a locale.
    default_language: Mapped[Language] = mapped_column(
        language_type(), default=Language.EN, server_default=Language.EN.value
    )

    # Every language this tenant may be served in, default included - migration 0011's
    # ck_tenants_supported_languages refuses a default the tenant does not support. Plain
    # strings: the database guards the values, and callers that need a member parse with
    # Language(code). The default is a callable so every new tenant gets its own list.
    supported_languages: Mapped[list[str]] = mapped_column(
        ARRAY(String(8)),
        default=lambda: [Language.EN.value],
        server_default=text("'{en}'"),
    )

    # Tokens (prompt + completion, summed over agent_runs) this tenant may spend per calendar
    # day in settings.token_budget_timezone (EAP-64). Checked before every model call; the
    # run endpoint answers 429 once it is gone. 0 means no cap - an explicit operator
    # choice, never the default: a spend control that defaults to off is a bill, not a limit.
    daily_token_budget: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_DAILY_TOKEN_BUDGET, server_default=str(DEFAULT_DAILY_TOKEN_BUDGET)
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.slug}>"
