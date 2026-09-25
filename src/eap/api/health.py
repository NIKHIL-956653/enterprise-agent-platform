"""Liveness and readiness probes."""

import time

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from eap.core.db import get_session_factory
from eap.core.redis import get_redis

router = APIRouter(tags=["health"])

# EAP-10: a probe must be cheaper than the thing it probes.
#
# Checking dependencies on every call takes a pooled connection each time. Under load every
# connection is serving real requests, so the probe blocks, times out, and the orchestrator
# removes a pod that was merely busy. Its traffic moves to the others, which saturate
# faster and fail too - a cascading outage caused by the monitoring.
#
# Caching for a few seconds bounds that cost. The trade-off is up to TTL seconds of delay
# before a dependency coming back is noticed, which is far cheaper than the failure mode.
_CACHE_TTL_SECONDS = 5.0
_cache: tuple[float, dict[str, str]] | None = None


def reset_readiness_cache() -> None:
    """Clear the cache. Tests call this so one test's result cannot leak into the next."""
    global _cache
    _cache = None


async def _probe_dependencies() -> dict[str, str]:
    checks: dict[str, str] = {}
    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["postgres"] = f"error: {type(e).__name__}"
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["redis"] = f"error: {type(e).__name__}"
    return checks


@router.get("/livez")
async def livez() -> dict:
    """
    Is this process alive?

    Touches NOTHING. If liveness checked the database, a 30-second outage would make the
    orchestrator restart every pod at once - they all share the database, so they all fail
    together - and the database would then be hit by a fleet of cold starts. A restart
    storm caused purely by a monitoring choice.
    """
    return {"status": "alive"}


@router.get("/readyz")
async def readyz(response: Response) -> dict:
    """
    Should this instance receive traffic right now?

    EAP-9: answers 503 when degraded. Load balancers and Kubernetes read the STATUS CODE,
    not the body - returning 200 with {"status": "degraded"} means traffic keeps arriving
    at an instance that cannot serve it.
    """
    global _cache
    now = time.monotonic()
    if _cache is None or (now - _cache[0]) > _CACHE_TTL_SECONDS:
        _cache = (now, await _probe_dependencies())
    checks = _cache[1]

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if healthy else "degraded", "checks": checks}
