"""
Health probe tests.

These are integration tests on purpose: they run against the real Postgres and Redis
from docker-compose. Mocking the DB here would only prove the mock works.
"""

import pytest
from httpx import AsyncClient

from eap.api import health


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


async def test_readyz_returns_503_when_degraded(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    EAP-9. The body already said "degraded"; the status code did not.

    Load balancers route on the status code, so 200 here means traffic keeps arriving at an
    instance that cannot serve it. This test fails if anyone reverts that.
    """

    def _broken_redis() -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(health, "get_redis", _broken_redis)
    health.reset_readiness_cache()

    r = await client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["checks"]["redis"].startswith("error")


async def test_readyz_caches_its_probes(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    EAP-10. Three probes in quick succession must hit the dependencies once.

    Without this, a load balancer polling every 2s across N pods takes a pooled connection
    every time - and under load that is exactly when there are none spare.
    """
    calls = 0

    async def _counting_probe() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"postgres": "ok", "redis": "ok"}

    monkeypatch.setattr(health, "_probe_dependencies", _counting_probe)
    health.reset_readiness_cache()

    for _ in range(3):
        r = await client.get("/readyz")
        assert r.status_code == 200

    assert calls == 1
