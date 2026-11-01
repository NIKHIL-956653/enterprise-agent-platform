"""
GET /v1/agents carries each agent's input schema (D5).

The admin UI renders every run form from this, so the test pins the contract the form
depends on: the schema IS the InputModel's own JSON Schema (not a hand-kept copy that could
drift), every implemented agent has one, and an agent with no code behind it has none.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from eap.agents.registry import get_agent, registered_names
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password

PASSWORD = "test-only-schema-password"


async def _tenant_with_every_agent_and_a_ghost() -> tuple[str, str]:
    slug = f"schema-{uuid.uuid4().hex[:8]}"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (gen_random_uuid(), :tid, 'a@x.edu', :ph)"
            ),
            {"tid": tid, "ph": hash_password(PASSWORD)},
        )
        # "ghost" is enabled in the database but no code serves it - e.g. after a rollback.
        for name in [*registered_names(), "ghost"]:
            await s.execute(
                text(
                    "INSERT INTO agents (id, tenant_id, name, display_name) "
                    "VALUES (gen_random_uuid(), :tid, :n, :n)"
                ),
                {"tid": tid, "n": name},
            )
        await s.commit()
    return slug, "a@x.edu"


async def _agents(client: AsyncClient) -> dict[str, dict]:
    slug, email = await _tenant_with_every_agent_and_a_ghost()
    r = await client.post("/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": PASSWORD})
    r = await client.get("/v1/agents", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert r.status_code == 200, r.text
    return {a["name"]: a for a in r.json()}


async def test_every_implemented_agent_carries_its_own_input_schema(client: AsyncClient) -> None:
    agents = await _agents(client)

    for name in registered_names():
        assert agents[name]["has_implementation"] is True
        assert agents[name]["input_schema"] == get_agent(name).InputModel.model_json_schema(), name


async def test_an_agent_without_code_has_no_schema(client: AsyncClient) -> None:
    agents = await _agents(client)

    assert agents["ghost"]["has_implementation"] is False
    assert agents["ghost"]["input_schema"] is None


async def test_echo_takes_a_plain_text_message(client: AsyncClient) -> None:
    # The demo bug: echo showed a raw JSON box that rejected plain text. Its schema says
    # what the form must render instead - one required string field.
    schema = (await _agents(client))["echo"]["input_schema"]

    assert schema["required"] == ["message"]
    assert schema["properties"]["message"]["type"] == "string"
    assert schema["properties"]["message"]["minLength"] == 1
