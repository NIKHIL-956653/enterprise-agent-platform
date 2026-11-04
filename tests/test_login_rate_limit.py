"""
EAP-69: login attempts and document ingests are rate limited; runs already were (EAP-46).

Login has two counters. Per (tenant, email) so a dictionary attack on one account gets ten
argon2 verifications a minute, not thousands; per source IP so one password cannot be sprayed
across every account from one place. The per-IP test builds its own client with its own
address, because every other test in the suite arrives from the same one (see conftest).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from eap.core.config import get_settings
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.main import app
from eap.rag import ingest
from tests.test_retrieval import RankingFakeEmbedder

PASSWORD = "correct-horse-battery-staple"


async def _tenant(users: int = 1) -> dict:
    slug = f"rl-{uuid.uuid4().hex[:8]}"
    emails = [f"{uuid.uuid4().hex[:8]}@example.com" for _ in range(users)]
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()
        for email in emails:
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash) "
                    "VALUES (gen_random_uuid(), :tid, :email, :ph)"
                ),
                {"tid": tid, "email": email, "ph": hash_password(PASSWORD)},
            )
        await s.commit()
    return {"slug": slug, "emails": emails}


async def _login(client: AsyncClient, slug: str, email: str, password: str = PASSWORD):
    return await client.post(
        "/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": password}
    )


# --- per account -----------------------------------------------------------------------------


@pytest.fixture
def three_per_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_ACCOUNT_PER_MINUTE", "3")
    get_settings.cache_clear()


async def test_the_fourth_attempt_on_one_account_is_refused(
    client: AsyncClient, three_per_account: None
) -> None:
    t = await _tenant()
    email = t["emails"][0]

    for _ in range(3):
        assert (await _login(client, t["slug"], email, "wrong")).status_code == 401

    # Even the right password: the window is closed for this account, and the response
    # carries when it reopens but not how many tries were left.
    r = await _login(client, t["slug"], email)
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) > 0
    assert "x-ratelimit-limit" not in {k.lower() for k in r.headers}


async def test_one_account_cannot_lock_out_another(client: AsyncClient, three_per_account: None) -> None:
    t = await _tenant(users=2)
    victim, other = t["emails"]

    for _ in range(4):
        await _login(client, t["slug"], victim, "wrong")

    assert (await _login(client, t["slug"], victim)).status_code == 429
    assert (await _login(client, t["slug"], other)).status_code == 200


async def test_the_account_key_is_case_insensitive(client: AsyncClient, three_per_account: None) -> None:
    # EMAIL@x and email@x are one account; varying the case must not buy more attempts.
    t = await _tenant()
    email = t["emails"][0]
    for variant in (email, email.upper(), email.title()):
        await _login(client, t["slug"], variant, "wrong")

    assert (await _login(client, t["slug"], email)).status_code == 429


async def test_zero_disables_the_account_limit(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_ACCOUNT_PER_MINUTE", "0")
    get_settings.cache_clear()
    t = await _tenant()
    for _ in range(12):
        assert (await _login(client, t["slug"], t["emails"][0], "wrong")).status_code == 401


# --- per source address ----------------------------------------------------------------------


@pytest.fixture
async def own_address_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """A client with a client address no other test uses, and the per-IP limit at 3."""
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_IP_PER_MINUTE", "3")
    monkeypatch.setenv("RATE_LIMIT_LOGIN_PER_ACCOUNT_PER_MINUTE", "0")
    get_settings.cache_clear()
    address = f"10.{uuid.uuid4().int % 250 + 1}.{uuid.uuid4().int % 250 + 1}.{uuid.uuid4().int % 250 + 1}"
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, client=(address, 54321))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_spraying_across_accounts_from_one_address_is_refused(own_address_client: AsyncClient) -> None:
    t = await _tenant(users=4)
    client = own_address_client

    for email in t["emails"][:3]:
        assert (await _login(client, t["slug"], email, "wrong")).status_code == 401

    r = await _login(client, t["slug"], t["emails"][3])
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) > 0


# --- document ingest -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest, "_default_embedder", lambda: RankingFakeEmbedder())


async def _ingest(client: AsyncClient, token: str, i: int):
    return await client.post(
        "/v1/documents",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": f"doc {i}", "body": "Residents park in the marina basement on level two."},
    )


async def test_the_third_ingest_in_a_minute_is_refused(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_DOCUMENTS_PER_MINUTE", "2")
    get_settings.cache_clear()
    t = await _tenant()
    token = (await _login(client, t["slug"], t["emails"][0])).json()["access_token"]

    assert (await _ingest(client, token, 1)).status_code == 201
    second = await _ingest(client, token, 2)
    assert second.status_code == 201
    assert second.headers["X-RateLimit-Remaining"] == "0"

    third = await _ingest(client, token, 3)
    assert third.status_code == 429, third.text
    assert int(third.headers["Retry-After"]) > 0


async def test_one_tenant_cannot_exhaust_anothers_ingest_quota(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_DOCUMENTS_PER_MINUTE", "1")
    get_settings.cache_clear()
    noisy, quiet = await _tenant(), await _tenant()
    tn = (await _login(client, noisy["slug"], noisy["emails"][0])).json()["access_token"]
    tq = (await _login(client, quiet["slug"], quiet["emails"][0])).json()["access_token"]

    assert (await _ingest(client, tn, 1)).status_code == 201
    assert (await _ingest(client, tn, 2)).status_code == 429
    assert (await _ingest(client, tq, 1)).status_code == 201
