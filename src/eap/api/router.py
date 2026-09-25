"""Top-level API router - every feature router is included here, versioned under /v1."""

from fastapi import APIRouter

from eap.api import agents, auth, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router, prefix="/v1")
api_router.include_router(agents.router, prefix="/v1")
