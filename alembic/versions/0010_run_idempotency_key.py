"""idempotency key on agent runs

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable on purpose: idempotency is the CLIENT's choice. A caller that sends no key
    # gets a fresh run per POST, exactly as before. A caller that sends one gets the same
    # run back however many times its retry logic fires.
    op.add_column("agent_runs", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    # Partial unique index: NULLs stay free (every keyless run may coexist), duplicates of
    # a real key are refused by the DATABASE - the race between two concurrent retries is
    # settled here, not in application code that both requests run simultaneously.
    op.create_index(
        "uq_agent_runs_tenant_idempotency",
        "agent_runs",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_runs_tenant_idempotency", table_name="agent_runs")
    op.drop_column("agent_runs", "idempotency_key")
