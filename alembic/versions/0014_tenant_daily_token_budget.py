"""daily token budget per tenant

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# Frozen here rather than imported from eap.models.tenant, for the reason 0011 gives: a
# migration records what shipped. Changing the default later is a new migration.
DEFAULT = 500_000


def upgrade() -> None:
    # EAP-64. EAP-46's limiter counts requests per minute; nothing counted tokens, and
    # prompt_tokens/completion_tokens on agent_runs were never filled in. This column is the
    # cap; the runner starts writing the counts in the same change. NOT NULL with a default
    # so every existing tenant is capped the moment the migration lands.
    op.add_column(
        "tenants",
        sa.Column("daily_token_budget", sa.Integer(), nullable=False, server_default=str(DEFAULT)),
    )
    op.create_check_constraint(op.f("ck_tenants_daily_token_budget"), "tenants", "daily_token_budget >= 0")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_tenants_daily_token_budget"), "tenants", type_="check")
    op.drop_column("tenants", "daily_token_budget")
