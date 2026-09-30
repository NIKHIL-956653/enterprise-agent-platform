"""documents and document chunks

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# Must equal eap.models.document.EMBEDDING_DIMENSIONS. Written out rather than imported:
# a migration that reads application code changes meaning when that code changes, and a
# migration has to mean the same thing in five years as it did the day it ran.
EMBEDDING_DIMENSIONS = 768

TENANT_POLICY = """
CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
"""


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("source", sa.String(length=50), server_default="upload", nullable=False),
        sa.Column("meta", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_documents_tenant_id_tenants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("tenant_id", "external_id", name="uq_documents_tenant_id_external_id"),
    )
    op.create_index(op.f("ix_documents_tenant_id"), "documents", ["tenant_id"], unique=False)

    op.create_table(
        "document_chunks",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_document_chunks_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_id_ordinal"),
    )
    op.create_index(
        "ix_document_chunks_tenant_id_document_id", "document_chunks", ["tenant_id", "document_id"]
    )

    for table in ("documents", "document_chunks"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(TENANT_POLICY.format(table=table))


def downgrade() -> None:
    for table in ("document_chunks", "documents"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_document_chunks_tenant_id_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index(op.f("ix_documents_tenant_id"), table_name="documents")
    op.drop_table("documents")
