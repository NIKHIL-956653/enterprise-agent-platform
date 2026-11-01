"""application database role

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24
"""

from urllib.parse import unquote, urlsplit

from alembic import op
from sqlalchemy import text

from eap.core.config import get_settings

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
#
# The password (EAP-38) is not in this file. It is the one inside APP_DATABASE_URL - the URL
# the running app logs in with - so the role and the app cannot disagree, and nothing secret
# is committed. Unset, it is the dev default the file used to carry, so a local
# `docker compose up && alembic upgrade head` keeps working. It reaches the DO block through
# a transaction-local setting and format('%L'), never by string-building SQL. To rotate an
# existing deployment: ALTER ROLE eap_app PASSWORD '...', then change APP_DATABASE_URL.
DEFAULT_PASSWORD = "eap_app"


def app_role_password(app_database_url: object | None) -> str:
    """The password to create eap_app with: APP_DATABASE_URL's, else the dev default."""
    if app_database_url is None:
        return DEFAULT_PASSWORD
    raw = urlsplit(str(app_database_url)).password
    return unquote(raw) if raw else DEFAULT_PASSWORD


STATEMENTS = [
    """
    DO $do$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'eap_app') THEN
            EXECUTE format(
                'CREATE ROLE eap_app LOGIN PASSWORD %L',
                current_setting('eap.app_role_password')
            );
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
    # Transaction-local: gone when the migration's transaction ends, never logged as DDL.
    op.execute(
        text("SELECT set_config('eap.app_role_password', :pw, true)").bindparams(
            pw=app_role_password(get_settings().app_database_url)
        )
    )
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in REVERSALS:
        op.execute(stmt)
