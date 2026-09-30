"""
Per-tenant request rate limiting.

A fixed window counted in Redis, keyed by tenant and by window index.

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
from fastapi import Depends, HTTPException, Response, status
from redis.exceptions import RedisError

from eap.core.config import get_settings
from eap.core.deps import get_current_tenant_id
from eap.core.redis import get_redis

log = structlog.get_logger(__name__)

WINDOW_SECONDS = 60


async def _hit(tenant_id: UUID) -> int:
    """Count this request and return the running total for the current window."""
    window = int(time.time()) // WINDOW_SECONDS
    key = f"rl:runs:{tenant_id}:{window}"

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


async def enforce_run_rate_limit(
    response: Response,
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
) -> None:
    """
    Reject a tenant that has exceeded its per-minute budget for this endpoint.

    Counting happens before the work, not after: the point is to refuse cheaply.
    """
    limit = get_settings().rate_limit_runs_per_minute
    if limit <= 0:
        # 0 disables it. Explicit rather than clever, so a test or a local box can turn it off
        # without monkeypatching the module.
        return

    try:
        count = await _hit(tenant_id)
    except RedisError as exc:
        # FAIL OPEN, deliberately. This limiter protects availability; refusing every request
        # because Redis is unreachable converts a cache outage into a total outage. Logged at
        # warning so the gap is visible rather than silent.
        #
        # The per-tenant COST cap (EAP-49) is the opposite call: that is a spend control, and a
        # spend control that fails open is how you discover a bill instead of a limit.
        log.warning("rate_limit_unavailable", tenant_id=str(tenant_id), error=str(exc))
        return

    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(max(limit - count, 0))

    if count > limit:
        # Seconds until this window rolls over — a real number, not a fixed guess, so a polite
        # client waits exactly as long as it has to.
        retry_after = WINDOW_SECONDS - int(time.time()) % WINDOW_SECONDS
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
        )
