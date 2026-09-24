"""
User - a person inside one tenant.

The first table to carry tenant_id, which is what RLS filters on. Everything about this
model exists to make cross-tenant access impossible rather than merely unlikely.
"""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap.models.base import Base
from eap.models.meta import Timestamps, UUIDPk


class UserRole(enum.StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class User(UUIDPk, Timestamps, Base):
    __tablename__ = "users"

    __table_args__ = (
        # Email is unique PER TENANT, not globally. The same person may legitimately have
        # an account at two customer companies. A global unique constraint would let one
        # tenant's signup block another tenant's - and leak that the email exists.
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
    )

    # ondelete CASCADE: deleting a tenant removes its users. Enforced by Postgres, so it
    # holds even if rows are deleted by a script that knows nothing about the app.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(320))

    # Never a password - only the argon2 hash. Named so that no one is tempted otherwise.
    password_hash: Mapped[str] = mapped_column(String(255))

    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            native_enum=False,
            length=20,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        default=UserRole.MEMBER,
        server_default=UserRole.MEMBER.value,
    )

    # Deactivate rather than delete: audit trails and agent runs must keep pointing at a
    # real user row.
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")

    def __repr__(self) -> str:
        return f"<User {self.email} tenant={self.tenant_id}>"
