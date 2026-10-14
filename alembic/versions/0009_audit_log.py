"""append-only audit log with per-tenant hash chain

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TENANT_POLICY = """
CREATE POLICY tenant_isolation ON audit_log
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
"""

# The database computes the chain, not the application - same philosophy as RLS: the
# guarantee has to sit below the code it is checking up on. An app that could pass its own
# seq and hash could also pass a consistent-looking lie.
#
# The advisory lock is not decoration. Two concurrent inserts for one tenant would both
# read the same last hash and fork the chain; the lock serialises audit writes per tenant
# for the duration of their transactions. hashtextextended returns the bigint the lock
# function wants.
CHAIN_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_chain_link() RETURNS trigger AS $$
DECLARE
    last record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text, 0));

    SELECT seq, hash INTO last
      FROM audit_log
     WHERE tenant_id = NEW.tenant_id
     ORDER BY seq DESC
     LIMIT 1;

    NEW.seq := COALESCE(last.seq, 0) + 1;
    NEW.prev_hash := COALESCE(last.hash, repeat('0', 64));
    NEW.hash := encode(digest(
        NEW.prev_hash || NEW.tenant_id::text || NEW.seq::text || NEW.event_type
        || NEW.payload::text || COALESCE(NEW.run_id::text, '') || NEW.created_at::text,
        'sha256'), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    # digest() lives in pgcrypto. Shipped with Postgres, but an extension all the same.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "audit_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_audit_log_tenant_id_tenants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
        sa.UniqueConstraint("tenant_id", "seq", name="uq_audit_log_tenant_id_seq"),
    )
    op.create_index(op.f("ix_audit_log_tenant_id"), "audit_log", ["tenant_id"], unique=False)

    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_log FORCE ROW LEVEL SECURITY")
    op.execute(TENANT_POLICY)

    op.execute(CHAIN_FUNCTION)
    op.execute(
        "CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_chain_link()"
    )

    # THE line this table exists for. Migration 0005's default privileges hand eap_app
    # UPDATE and DELETE on every new table; left in place here, "append-only" would be a
    # comment, not a property. After this, tampering requires the owner role - possible,
    # but the chain makes it detectable, and detectable is the compliance requirement.
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM eap_app")

    # No UPDATE trigger guard for the owner: a role that can UPDATE can also drop the
    # guard. The chain, not a trigger, is what catches an owner-level edit.


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_chain ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_chain_link()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_log")
    op.drop_index(op.f("ix_audit_log_tenant_id"), table_name="audit_log")
    op.drop_table("audit_log")
