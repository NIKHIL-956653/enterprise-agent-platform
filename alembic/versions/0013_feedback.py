"""answer feedback, one vote per user per run

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# Same policy, same shape, as every tenant table since 0003.
TENANT_POLICY = """
CREATE POLICY tenant_isolation ON feedback
    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
"""


def upgrade() -> None:
    # The composite foreign key below needs (tenant_id, id) to be a key on agent_runs. id is
    # already unique, so this constraint adds no information - it exists so Postgres can
    # check "this run belongs to this tenant" for us on every feedback insert.
    op.create_unique_constraint("uq_agent_runs_tenant_id_id", "agent_runs", ["tenant_id", "id"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("verdict", sa.String(length=8), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_feedback_tenant_id_tenants"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_feedback_user_id_users"), ondelete="CASCADE"
        ),
        # EAP-63: a vote's tenant must be its run's tenant, checked by the database. RLS
        # forces tenant_id to the caller's own; a run from another tenant then has no
        # matching (tenant_id, id) parent and the insert fails, whatever the handler does.
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["agent_runs.tenant_id", "agent_runs.id"],
            name=op.f("fk_feedback_tenant_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("run_id", "user_id", name="uq_feedback_run_id_user_id"),
    )
    op.create_index(op.f("ix_feedback_tenant_id"), "feedback", ["tenant_id"], unique=False)

    # By name and by hand, like the language CHECKs in 0011: the model's String column
    # emits none, and 'sideways' must fail from raw SQL as surely as from the API.
    op.create_check_constraint(op.f("ck_feedback_verdict"), "feedback", "verdict IN ('up', 'down')")

    op.execute("ALTER TABLE feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE feedback FORCE ROW LEVEL SECURITY")
    op.execute(TENANT_POLICY)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON feedback")
    op.drop_constraint(op.f("ck_feedback_verdict"), "feedback", type_="check")
    op.drop_index(op.f("ix_feedback_tenant_id"), table_name="feedback")
    op.drop_table("feedback")
    op.drop_constraint("uq_agent_runs_tenant_id_id", "agent_runs", type_="unique")
