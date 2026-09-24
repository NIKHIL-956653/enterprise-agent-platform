"""
Model registry.

Importing every model here is what puts it on Base.metadata. Alembic autogenerate
compares the database against that metadata - a model that is never imported is
invisible to it, and you get a silent empty migration instead of an error.

Rule: every new model file gets a line here, in the same commit.
"""

from eap.models.base import Base
from eap.models.tenant import Tenant, TenantStatus
from eap.models.user import User, UserRole

__all__ = ["Base", "Tenant", "TenantStatus", "User", "UserRole"]
