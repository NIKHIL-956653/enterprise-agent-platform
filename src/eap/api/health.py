"""Liveness and readiness probes."""

from fastapi import APIRouter
from sqlalchemy import text

from eap.core.db import get_session_factory
from eap.core.redis import get_redis

router = APIRouter(tags=["health"])


@router.get("/livez")
async def livez() -> dict:
    return {"status": "alive"}


@router.get("/readyz")
async def readyz() -> dict:
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
    healthy = all(v == "ok" for v in checks.values())
    return {"status": "ready" if healthy else "degraded", "checks": checks}
