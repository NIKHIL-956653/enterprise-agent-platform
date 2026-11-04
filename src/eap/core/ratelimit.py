"""
Request rate limiting: a fixed window counted in Redis, keyed by a caller-specific key and
the window index.

Three enforcers share the counter (EAP-46, EAP-69): runs per tenant, document ingests per
tenant, and login attempts per source IP and per (tenant, email). Login is the odd one out -
it runs BEFORE any token exists, so it is called from the handler with the request's own
fields rather than resolved as a dependency.

It is a DEPENDENCY, not middleware. The tenant only exists once the bearer token has been
verified, and middleware runs before that — so a middleware limiter would have to decode the
token a second time, or fall back to limiting by IP, which buckets every tenant behind one
corporate NAT into a single quota.

The key carries the window index (`epoch // window`), so every window is a NEW key. That
removes the classic fixed-window bug: if you refresh the TTL on every request, a continuously
busy tenant keeps its key alive forever, the window never rolls over, and the tenant stays
blocked permanently. A fresh key per window cannot do that, and it needs neither Lua nor
Redis 7's `EXPIRE NX`.

Known property, accepted: a caller can land `limit` requests at the end of one window and
`limit` more at the start of the next, so the worst case over an arbitrary sliding minute is
2x the limit. The fix is a sliding-window counter, and it is not worth the complexity until
this is protecting something tighter than a dev box. Written down rather than discovered later.
"""

import time
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request, Response, status
from redis.exceptions import RedisError

from eap.core.config import get_settings
from eap.core.deps import get_current_tenant_id
from eap.core.redis import get_redis

log = structlog.get_logger(__name__)

WINDOW_SECONDS = 60


async def _hit(key: str) -> int:
    """Count this request under `key` and return the running total for the current window."""
    window = int(time.time()) // WINDOW_SECONDS
    key = f"{key}:{window}"

    # MULTI/EXEC, so the counter can never be incremented without also being given a TTL.
    # A bare INCR whose EXPIRE is lost to a crash leaves an immortal key — which is a tenant
    # locked out until someone notices and deletes it by hand.
    async with get_redis().pipeline(transaction=True) as pipe:
        pipe.incr(key)
        # Twice the window: generous enough that clock skew or a slow request cannot orphan
        # the count, while still well gone before that window index comes round again.
        pipe.expire(key, WINDOW_SECONDS * 2)
        count, _ = await pipe.execute()

    return int(count)


async def _enforce(key: str, limit: int, response: Response | None, *, what: str) -> None:
    """
    Count one request under `key` and refuse it with 429 once `limit` is exceeded.

    Counting happens before the work, not after: the point is to refuse cheaply. With a
    Response, the X-RateLimit headers are set on it; login passes None, so a failed login
    says nothing about how many more tries the attacker has.
    """
    if limit <= 0:
        # 0 disables it. Explicit rather than clever, so a test or a local box can turn it off
        # without monkeypatching the module.
        return

    try:
        count = await _hit(key)
    except RedisError as exc:
        # FAIL OPEN, deliberately. This limiter protects availability; refusing every request
        # because Redis is unreachable converts a cache outage into a total outage. Logged at
        # warning so the gap is visible rather than silent.
        #
        # The per-tenant COST cap (EAP-64) is the opposite call: that is a spend control, and a
        # spend control that fails open is how you discover a bill instead of a limit.
        log.warning("rate_limit_unavailable", limiter=what, error=str(exc))
        return

    if response is not None:
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(limit - count, 0))

    if count > limit:
        # Seconds until this window rolls over — a real number, not a fixed guess, so a polite
        # client waits exactly as long as it has to.
        retry_after = WINDOW_SECONDS - int(time.time()) % WINDOW_SECONDS
        headers = {"Retry-After": str(retry_after)}
        if response is not None:
            headers.update({"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers=headers,
        )


async def enforce_run_rate_limit(
    response: Response,
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
) -> None:
    """Runs per tenant per minute (EAP-46)."""
    await _enforce(f"rl:runs:{tenant_id}", get_settings().rate_limit_runs_per_minute, response, what="runs")


async def enforce_document_rate_limit(
    response: Response,
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
) -> None:
    """Document ingests per tenant per minute (EAP-69): every call spends embedding money."""
    await _enforce(
        f"rl:docs:{tenant_id}", get_settings().rate_limit_documents_per_minute, response, what="documents"
    )


async def enforce_login_rate_limit(request: Request, tenant_slug: str, email: str) -> None:
    """
    Login attempts per source IP and per (tenant, email), per minute (EAP-69).

    Two keys on purpose. The account key stops a targeted guess at one user - ten tries a
    minute against argon2 is nothing to a person and everything to a dictionary. The IP key
    stops spraying one password across many accounts from one place; it is the looser of
    the two because a whole college can sit behind one NAT. Behind Caddy the IP is the real
    client's: uvicorn runs with --proxy-headers, and only Caddy can reach the API port
    (docs/BACKLOG.md, EAP-74). Every attempt counts, successful or not; a person does not
    log in ten times a minute, a script does.
    """
    s = get_settings()
    ip = request.client.host if request.client is not None else "unknown"
    await _enforce(f"rl:login:ip:{ip}", s.rate_limit_login_per_ip_per_minute, None, what="login-ip")
    account = f"{tenant_slug.strip().lower()}:{email.strip().lower()}"
    await _enforce(
        f"rl:login:acct:{account}", s.rate_limit_login_per_account_per_minute, None, what="login-account"
    )
