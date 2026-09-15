"""
Request-id middleware: every request gets an id (from X-Request-ID if the caller sent one),
it is bound into the structlog context for the life of the request and echoed back in the
response. Also logs one access line per request with method, path, status and latency.
"""

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

log = structlog.get_logger("eap.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        response.headers["x-request-id"] = request_id
        log.info("request", method=request.method, path=request.url.path, status=response.status_code, ms=ms)
        return response
