"""App factory: logging → middleware → routes, with clean startup/shutdown."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from eap.api.router import api_router
from eap.core.config import get_settings
from eap.core.db import dispose_engine, get_engine
from eap.core.logging import configure_logging, get_logger
from eap.core.middleware import RequestContextMiddleware
from eap.core.redis import close_redis, get_redis

log = get_logger("eap")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_logging(level=s.log_level, json_logs=s.is_prod)
    get_engine()
    get_redis()
    log.info("startup", env=s.app_env)
    yield
    await dispose_engine()
    await close_redis()
    log.info("shutdown")


def create_app() -> FastAPI:
    get_settings()  # fail fast: bad config raises here, before the server binds a port
    app = FastAPI(title="Enterprise Agent Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_router)
    return app


app = create_app()
