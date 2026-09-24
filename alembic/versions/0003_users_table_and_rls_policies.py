"""users table and rls policies

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# current_setting(..., true) returns NULL when the variable is not set, so the comparison
# is NULL and NO rows match. A request that forgot to set the tenant sees nothing rather
# than everything - the safe direction to fail in.
TENANT_POLICY = """
CREATE POLICY tenant_isolation ON {table}
    USING ({column} = current_setting('app.tenant_id', true)::uuid)
    WITH CHECK ({column} = current_setting('app.tenant_id', true)::uuid)
"""


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", "member", name="userrole", native_enum=False, length=20),
            server_default="member",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_users_tenant_id_tenants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
    )
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)

    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(TENANT_POLICY.format(table="users", column="tenant_id"))

    # tenants got ENABLE + FORCE in 0002 but no policy, which meant deny-everything.
    # This opens it deliberately: a tenant may read its own row and no other.
    op.execute(TENANT_POLICY.format(table="tenants", column="id"))


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenants")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON users")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_table("users")
