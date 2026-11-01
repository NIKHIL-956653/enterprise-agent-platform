"""document external_id unique per classification

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-01
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

OLD = "uq_documents_tenant_id_external_id"
NEW = "uq_documents_tenant_id_classification_external_id"


def upgrade() -> None:
    # EAP-61, ADR-006. With (tenant_id, external_id) unique across classifications, a
    # caller posting an id that existed above their clearance hit ON CONFLICT DO UPDATE on a
    # row RLS hides from them; the policy refused it, and the handler said "classification
    # exceeds your clearance". A 403 for a taken id and a 201 for a free one is an existence
    # oracle for restricted documents. With classification in the key, a re-post only ever
    # conflicts with a row at the level being written - one the caller can see.
    op.drop_constraint(OLD, "documents", type_="unique")
    op.create_unique_constraint(NEW, "documents", ["tenant_id", "classification", "external_id"])


def downgrade() -> None:
    # Reversible only while no tenant holds one external_id at two classifications. If one
    # does, this fails on the unique violation and says so, rather than deleting anyone's
    # documents to restore the old invariant. Consolidate first: re-post the document at the
    # classification to keep, as a user who can see every copy.
    op.drop_constraint(NEW, "documents", type_="unique")
    op.create_unique_constraint(OLD, "documents", ["tenant_id", "external_id"])
