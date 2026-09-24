"""tenant slug lookup function

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Login has a chicken-and-egg problem: it must find the tenant before it can set
# app.tenant_id, but the RLS policy on tenants needs app.tenant_id already set.
#
# SECURITY DEFINER runs the function as its owner, so it bypasses RLS - but only for this
# one query, which returns a single uuid and nothing else. The alternative (a policy
# allowing unauthenticated SELECT on tenants) would expose the whole customer list.
#
# SET search_path is mandatory on SECURITY DEFINER functions: without it a caller can
# put a malicious "tenants" table earlier on their search_path and have it read as owner.
CREATE_FN = """
CREATE OR REPLACE FUNCTION tenant_id_for_slug(p_slug text)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
    SELECT id FROM tenants WHERE slug = p_slug AND status = 'active'
$fn$
"""


def upgrade() -> None:
    op.execute(CREATE_FN)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS tenant_id_for_slug(text)")
