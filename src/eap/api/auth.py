"""
Authentication endpoints.

Login takes a tenant slug as well as an email, because email is only unique WITHIN a
tenant - the same person may hold accounts at two customer organisations.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.db import get_session
from eap.core.deps import get_claims, get_tenant_session
from eap.core.jwt import create_access_token
from eap.core.security import verify_password
from eap.models.tenant import Tenant
from eap.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=63)
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    user_id: UUID
    tenant_id: UUID
    tenant_slug: str
    email: EmailStr
    role: str


# One message for every failure. Telling the caller whether the tenant, the email or the
# password was wrong turns the login form into a directory of who your customers are.
_INVALID = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenResponse:
    # Step 1: resolve the tenant. SECURITY DEFINER function, because the RLS policy on
    # tenants needs app.tenant_id and we do not have it yet (migration 0004).
    result = await session.execute(text("SELECT tenant_id_for_slug(:slug)"), {"slug": body.tenant_slug})
    tenant_id = result.scalar_one_or_none()
    if tenant_id is None:
        raise _INVALID

    # Step 2: adopt that tenant for this transaction. Every query below is RLS-filtered.
    await session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})

    user = (await session.execute(select(User).where(User.email == body.email))).scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise _INVALID

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=MeResponse)
async def me(
    claims: Annotated[dict[str, Any], Depends(get_claims)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> MeResponse:
    """
    The caller's own identity.

    Note what is NOT here: no tenant filter in either query. The session already carries
    app.tenant_id from the token, so RLS applies it. If a token somehow named a user from
    another tenant, that row would simply not exist as far as this query is concerned.
    """
    user = (await session.execute(select(User).where(User.id == UUID(claims["sub"])))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise _INVALID

    tenant = (await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))).scalar_one()

    return MeResponse(
        user_id=user.id,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        email=user.email,
        role=user.role.value,
    )
