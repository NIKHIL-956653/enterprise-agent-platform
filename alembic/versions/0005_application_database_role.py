"""application database role

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Postgres superusers bypass RLS entirely - FORCE does not stop them. So an isolation test
# run as the owner would pass while proving nothing.
#
# The app therefore connects as eap_app: it can read and write the tables but owns none of
# them, so every policy applies to it. Migrations keep running as the owner.
#
# Roles are cluster-level, so creating one in a migration is a pragmatic choice that keeps
# dev and CI identical. In production, roles are provisioned by infrastructure instead.
#
# NOTE: asyncpg sends one statement per execute, so each statement is its own op.execute.
STATEMENTS = [
    """
    DO $do$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eap_app') THEN
            CREATE ROLE eap_app LOGIN PASSWORD 'eap_app';
        END IF;
    END
    $do$
    """,
    """
    DO $do$
    BEGIN
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO eap_app', current_database());
    END
    $do$
    """,
    "GRANT USAGE ON SCHEMA public TO eap_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO eap_app",
    "GRANT EXECUTE ON FUNCTION tenant_id_for_slug(text) TO eap_app",
    # Tables created by later migrations must be reachable without a new GRANT each time.
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO eap_app",
]

REVERSALS = [
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM eap_app",
    "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM eap_app",
    "REVOKE ALL ON SCHEMA public FROM eap_app",
    "DROP ROLE IF EXISTS eap_app",
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in REVERSALS:
        op.execute(stmt)
