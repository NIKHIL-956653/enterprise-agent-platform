"""
Authentication endpoints.

Login takes a tenant slug as well as an email, because email is only unique WITHIN a
tenant - the same person may hold accounts at two customer organisations.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.db import get_session
from eap.core.jwt import create_access_token
from eap.core.security import verify_password
from eap.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=63)
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# One message for every failure. Telling the caller whether the tenant, the email or the
# password was wrong turns the login form into a directory of who banks with you.
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
    # Step 1: resolve the tenant. SECURITY DEFINER function, because RLS on tenants needs
    # app.tenant_id and we do not have it yet (see migration 0004).
    result = await session.execute(text("SELECT tenant_id_for_slug(:slug)"), {"slug": body.tenant_slug})
    tenant_id = result.scalar_one_or_none()
    if tenant_id is None:
        raise _INVALID

    # Step 2: adopt that tenant for the rest of this transaction. Every query from here on
    # - including the user lookup below - is filtered by RLS. set_config(..., true) makes
    # it transaction-local, so it cannot leak to the next request on a pooled connection.
    await session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})

    user = (await session.execute(select(User).where(User.email == body.email))).scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise _INVALID

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id, role=user.role.value)
    return TokenResponse(access_token=token)
