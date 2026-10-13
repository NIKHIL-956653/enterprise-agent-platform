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


async def get_current_clearance(claims: Annotated[dict[str, Any], Depends(get_claims)]) -> int:
    """
    How sensitive a document this caller may read. From the verified token only, same rule
    as the tenant - a caller who can name their own clearance has none.

    A token minted before EAP-40 carries no `clearance` claim and reads as 0, the lowest
    level. That is the fail-closed direction: an old token keeps working and sees public
    documents, never restricted ones.
    """
    try:
        return int(claims.get("clearance", 0))
    except (TypeError, ValueError) as e:
        raise _UNAUTHORIZED from e


async def get_tenant_session(
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
    clearance: Annotated[int, Depends(get_current_clearance)],
) -> AsyncIterator[AsyncSession]:
    """
    A session whose every query is already filtered to one tenant and one clearance level.

    set_config(..., true) makes both settings TRANSACTION-local. That third argument is the
    whole safety story: a session-local setting would survive on a pooled connection and
    serve the previous request's context to the next one - the previous TENANT at worst,
    and a higher clearance than this caller has at best. Transaction-local dies with the
    transaction, every time, even on an exception.
    """
    async with get_session_factory()() as session:
        try:
            await session.execute(
                text(
                    "SELECT set_config('app.tenant_id', :tid, true), "
                    "       set_config('app.clearance', :clr, true)"
                ),
                {"tid": str(tenant_id), "clr": str(clearance)},
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
