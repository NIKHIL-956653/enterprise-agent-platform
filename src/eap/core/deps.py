"""
Request dependencies.

This module is where a bearer token becomes a tenant-scoped database session. It is the
one place app.tenant_id is ever set - everything else just asks for a session and gets
one that is already filtered (ADR-002).
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.db import get_session_factory
from eap.core.jwt import TokenError, decode_access_token

# auto_error=False so a missing header raises OUR 401, not Starlette's 403.
_bearer = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_claims(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict[str, Any]:
    """Verify the bearer token and return its claims, or 401."""
    if creds is None:
        raise _UNAUTHORIZED
    try:
        return decode_access_token(creds.credentials)
    except TokenError as e:
        raise _UNAUTHORIZED from e


async def get_current_tenant_id(claims: Annotated[dict[str, Any], Depends(get_claims)]) -> UUID:
    """
    The tenant this request acts for.

    Read ONLY from the verified token. Never from a body, query string or header - if a
    caller can name the tenant they want, there is no isolation.
    """
    try:
        return UUID(claims["tid"])
    except (KeyError, ValueError) as e:
        raise _UNAUTHORIZED from e


async def get_tenant_session(
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
) -> AsyncIterator[AsyncSession]:
    """
    A session whose every query is already filtered to one tenant.

    set_config(..., true) makes the setting TRANSACTION-local. That third argument is the
    whole safety story: a session-local setting would survive on a pooled connection and
    serve the previous tenant's context to the next request. Transaction-local dies with
    the transaction, every time, even on an exception.
    """
    async with get_session_factory()() as session:
        try:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
