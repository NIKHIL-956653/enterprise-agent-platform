"""document classification and user clearance

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Unset clearance reads as 0, the lowest level, never as NULL. With NULL the comparison
# `classification <= NULL` is null for every row, so a caller who forgot to set the context
# would get an empty corpus and read it as "nothing matched" rather than "misconfigured".
# Zero is the closed position in an ordered model - public documents are public.
# NULLIF covers the setting existing but holding an empty string.
CLEARANCE = "COALESCE(NULLIF(current_setting('app.clearance', true), '')::smallint, 0)"

CLASSIFIED_POLICY = f"""
CREATE POLICY tenant_isolation ON {{table}}
    USING (
        tenant_id = current_setting('app.tenant_id', true)::uuid
        AND classification <= {CLEARANCE}
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)::uuid
        AND classification <= {CLEARANCE}
    )
"""

# What 0007 had, restored on downgrade.
TENANT_ONLY_POLICY = """
CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
"""

CASCADE_FUNCTION = """
CREATE OR REPLACE FUNCTION sync_chunk_classification() RETURNS trigger AS $$
BEGIN
    IF NEW.classification IS DISTINCT FROM OLD.classification THEN
        UPDATE document_chunks
           SET classification = NEW.classification, updated_at = now()
         WHERE document_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.add_column("users", sa.Column("clearance", sa.SmallInteger(), nullable=False, server_default="0"))

    for table in ("documents", "document_chunks"):
        op.add_column(
            table, sa.Column("classification", sa.SmallInteger(), nullable=False, server_default="0")
        )
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(CLASSIFIED_POLICY.format(table=table))

    # A chunk carries its own classification for the same reason it carries its own
    # tenant_id: the policy runs against the chunk row and cannot follow document_id to
    # find it. Setting it at ingest is not enough - reclassifying a document later would
    # leave its chunks at the old level and retrieval would go on serving them, which is
    # the exact leak this migration exists to close. The trigger makes the invariant
    # structural rather than something every future writer has to remember.
    op.execute(CASCADE_FUNCTION)
    op.execute(
        "CREATE TRIGGER documents_classification_cascade "
        "AFTER UPDATE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION sync_chunk_classification()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS documents_classification_cascade ON documents")
    op.execute("DROP FUNCTION IF EXISTS sync_chunk_classification()")

    for table in ("documents", "document_chunks"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(TENANT_ONLY_POLICY.format(table=table))
        op.drop_column(table, "classification")

    op.drop_column("users", "clearance")
