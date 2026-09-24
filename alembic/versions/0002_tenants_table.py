"""tenants table

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "suspended", name="tenantstatus", native_enum=False, length=20),
            server_default="active",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
    )
    op.create_index(op.f("ix_tenants_slug"), "tenants", ["slug"], unique=True)

    # RLS is invisible to Alembic autogenerate - it lives in Postgres, not in SQLAlchemy
    # metadata, so every tenant table needs this written by hand.
    # FORCE matters: without it the table OWNER bypasses all policies, so isolation would
    # look enabled in dev (where you connect as the owner) while doing nothing at all.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenants FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_tenants_slug"), table_name="tenants")
    op.drop_table("tenants")
