"""index for listing a tenant's runs by language

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-03
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# GET /v1/runs?language=ar pages with the same keyset as the unfiltered list, so it wants
# the same index shape with language between tenant and time: every page is one range
# scan, whatever the page number (EAP-L4). Nothing changes on the column itself - 0011
# added it with its CHECK and 'en' backfill.
INDEX = "ix_agent_runs_tenant_id_language_created_at"


def upgrade() -> None:
    op.execute(f"CREATE INDEX {INDEX} ON agent_runs (tenant_id, language, created_at DESC)")


def downgrade() -> None:
    op.drop_index(INDEX, table_name="agent_runs")
