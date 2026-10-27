"""language on tenants, users, agent_runs and document_chunks

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# A frozen snapshot of eap.i18n.Language, deliberately NOT imported from it. A migration
# records the schema at one point in history. Importing app code would make this file change
# meaning the day the enum gains a member, and replaying it from an old checkout would build
# a different database than the one that shipped. A new language is a NEW migration that
# widens these CHECKs - which is also what makes adding one a reviewable change.
LANGUAGES = ("en", "ar")
LENGTH = 8  # A primary subtag today; room for a region or script subtag ("zh-Hant") later.

_IN = "(" + ", ".join(f"'{code}'" for code in LANGUAGES) + ")"
_ARRAY = "ARRAY[" + ", ".join(f"'{code}'" for code in LANGUAGES) + "]::varchar[]"

# Every CHECK is created by hand, with an explicit name. SQLAlchemy 2.x Enum(native_enum=False)
# does NOT emit a CHECK on its own (create_constraint defaults to False), so the model's
# Enum type validates in Python only. Raw SQL, a psql session or a future service in another
# language would sail straight past it. The database is the one place every writer meets.
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("ck_tenants_default_language", "tenants", f"default_language IN {_IN}"),
    (
        "ck_tenants_supported_languages",
        "tenants",
        # Non-empty, nothing unknown (a NULL element also fails <@), and the default must be
        # one of the supported - a tenant cannot default to a language it does not speak.
        f"cardinality(supported_languages) > 0 "
        f"AND supported_languages <@ {_ARRAY} "
        f"AND default_language = ANY(supported_languages)",
    ),
    ("ck_users_locale", "users", f"locale IS NULL OR locale IN {_IN}"),
    ("ck_agent_runs_language", "agent_runs", f"language IN {_IN}"),
    ("ck_document_chunks_language", "document_chunks", f"language IN {_IN}"),
)


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("default_language", sa.String(LENGTH), nullable=False, server_default="en"),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "supported_languages",
            postgresql.ARRAY(sa.String(LENGTH)),
            nullable=False,
            server_default=sa.text("'{en}'"),
        ),
    )

    # NULL means "follow the tenant's default". A user who never chose keeps tracking the
    # tenant if it later switches to Arabic, instead of being frozen at whatever the default
    # happened to be the day they were created.
    op.add_column("users", sa.Column("locale", sa.String(LENGTH), nullable=True))

    # Every existing run and chunk predates M6, and v1 was English-only, so 'en' is a true
    # backfill rather than a guess. ADD COLUMN with a constant default is a catalogue-only
    # change on Postgres 11+ - no table rewrite however many chunks exist. The server default
    # stays after the backfill so writers that predate EAP-L4/L5 keep working unchanged.
    for table in ("agent_runs", "document_chunks"):
        op.add_column(
            table,
            sa.Column("language", sa.String(LENGTH), nullable=False, server_default="en"),
        )

    # op.f() marks each name as final, so no naming convention can rewrite it. Tests look
    # these constraints up by name; a silently renamed one would read as a missing one.
    for name, table, condition in CHECKS:
        op.create_check_constraint(op.f(name), table, condition)


def downgrade() -> None:
    for name, table, _ in reversed(CHECKS):
        op.drop_constraint(op.f(name), table, type_="check")

    for table in ("document_chunks", "agent_runs"):
        op.drop_column(table, "language")
    op.drop_column("users", "locale")
    op.drop_column("tenants", "supported_languages")
    op.drop_column("tenants", "default_language")
