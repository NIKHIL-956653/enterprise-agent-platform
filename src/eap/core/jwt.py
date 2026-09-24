"""
Access tokens.

The token carries tenant_id. That single claim is what every later request trusts to know
which tenant it is acting for - it is never read from a body, query string or header,
because a caller must not be able to name the tenant they want (ADR-002).
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from eap.core.config import get_settings


class TokenError(Exception):
    """Raised for any invalid token: bad signature, expired, malformed, wrong claims."""


def create_access_token(*, user_id: UUID, tenant_id: UUID, role: str) -> str:
    """
    Mint a signed access token.

    sub  = the user (JWT convention)
    tid  = the tenant this token acts for
    role = coarse permission level, so common checks need no database round trip
    exp  = expiry. Short-lived on purpose: a leaked token stops working on its own,
           and it is the only revocation we have until a refresh-token store exists.
    """
    s = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=s.access_token_ttl_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """
    Verify and decode, or raise TokenError.

    algorithms is an explicit allow-list. Passing the header's own algorithm back to the
    decoder is the classic JWT vulnerability: an attacker sets alg to "none" or swaps
    HS256 for RS256 and the library validates their forgery happily.
    """
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e

    if "sub" not in payload or "tid" not in payload:
        raise TokenError("token missing sub or tid")
    return payload
