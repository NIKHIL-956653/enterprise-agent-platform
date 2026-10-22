"""
EAP-51: CORS for the admin UI.

A browser enforces CORS, not the server - the server's only job is to answer the preflight
honestly. So these tests check the answers: a listed origin is echoed back, an unlisted one
gets silence (which the browser turns into a block), and production refuses to boot with the
localhost default still in place.
"""

import pytest
from httpx import AsyncClient

from eap.core.config import Settings, get_settings

ALLOWED = "http://localhost:5173"
EVIL = "https://evil.example"


async def test_preflight_from_an_allowed_origin(client: AsyncClient) -> None:
    r = await client.options(
        "/v1/agents",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    # The UI sends the token in this header; a preflight that refuses it blocks every call.
    assert "authorization" in r.headers["access-control-allow-headers"].lower()


async def test_preflight_from_an_unlisted_origin_gets_no_allowance(client: AsyncClient) -> None:
    r = await client.options(
        "/v1/agents",
        headers={"Origin": EVIL, "Access-Control-Request-Method": "GET"},
    )
    # Starlette answers 400 with no allow-origin header; the browser does the blocking.
    assert "access-control-allow-origin" not in r.headers


async def test_simple_request_echoes_only_listed_origins(client: AsyncClient) -> None:
    r = await client.get("/livez", headers={"Origin": ALLOWED})
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "x-request-id" in r.headers.get("access-control-expose-headers", "").lower()

    r = await client.get("/livez", headers={"Origin": EVIL})
    assert "access-control-allow-origin" not in r.headers


def test_prod_refuses_the_localhost_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("GOOGLE_API_KEY", "set-for-prod-check")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="cors_allow_origins"):
            Settings()
        # And an explicit real origin boots fine - the guard targets the default, not CORS.
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["https://app.example.com"]')
        assert Settings().cors_allow_origins == ["https://app.example.com"]
    finally:
        get_settings.cache_clear()
