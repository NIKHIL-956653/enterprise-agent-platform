"""
Health probe tests.

These are integration tests on purpose: they run against the real Postgres and Redis
from docker-compose. Mocking the DB here would only prove the mock works.
"""

from httpx import AsyncClient


async def test_livez_is_alive(client: AsyncClient) -> None:
    """Liveness: 'is the process up?' Must not touch the DB — if it did, a dead
    database would make Kubernetes restart a perfectly healthy pod in a loop."""
    r = await client.get("/livez")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


async def test_readyz_reports_all_dependencies_ok(client: AsyncClient) -> None:
    """Readiness: 'can this process actually serve traffic?' Needs the compose stack up."""
    r = await client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"] == {"postgres": "ok", "redis": "ok"}
    assert body["status"] == "ready"


async def test_request_id_is_echoed_when_caller_supplies_one(client: AsyncClient) -> None:
    """A caller-supplied id must survive so one trace id spans several services."""
    r = await client.get("/livez", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["x-request-id"] == "trace-abc-123"


async def test_request_id_is_generated_when_absent(client: AsyncClient) -> None:
    """No header from the caller: we mint one, so every log line is still correlatable."""
    r = await client.get("/livez")
    assert len(r.headers["x-request-id"]) == 16
