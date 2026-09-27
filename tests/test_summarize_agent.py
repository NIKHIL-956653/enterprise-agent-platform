"""
Summarize agent through the full HTTP path, with the model faked.

No test here touches Gemini. The fake is injected at the one seam the runner exposes, so
these tests prove the platform's handling of an LLM - success, failure, absence - without
network, cost or nondeterminism. A real-Gemini smoke test belongs in a manual demo, not CI.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from eap.agents import runner
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.llm.base import LLMError

PASSWORD = "correct-horse-battery-staple"
TEXT = (
    "The platform runs many agents for many tenants. Each tenant's rows are isolated by "
    "row-level security, and every agent run is recorded whether it succeeds or fails."
)


class FakeLLM:
    """Records the prompt it was given and returns a canned answer or raises."""

    def __init__(self, answer: str = "A canned summary.", fail: bool = False) -> None:
        self.answer = answer
        self.fail = fail
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise LLMError("model call failed")
        return self.answer


async def _seed_tenant_with_summarize() -> dict[str, str]:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, :slug) RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (gen_random_uuid(), :tid, :email, :ph)"
            ),
            {"tid": tid, "email": email, "ph": hash_password(PASSWORD)},
        )
        await s.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, display_name) "
                "VALUES (gen_random_uuid(), :tid, 'summarize', 'Summarize')"
            ),
            {"tid": tid},
        )
        await s.commit()
    return {"slug": slug, "email": email}


async def _token(client: AsyncClient, seed: dict[str, str]) -> str:
    r = await client.post(
        "/v1/auth/login",
        json={"tenant_slug": seed["slug"], "email": seed["email"], "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _run(client: AsyncClient, token: str, payload: dict) -> dict:
    r = await client.post(
        "/v1/agents/summarize/run",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_summarize_succeeds_with_fake_llm(client, monkeypatch) -> None:
    fake = FakeLLM(answer="Agents run per tenant; every run is recorded.")
    monkeypatch.setattr(runner, "_default_llm", lambda: fake)
    token = await _token(client, await _seed_tenant_with_summarize())

    body = await _run(client, token, {"text": TEXT, "max_sentences": 2})

    assert body["status"] == "succeeded", body
    assert body["output"]["summary"] == fake.answer
    assert body["output"]["input_chars"] == len(TEXT)


async def test_prompt_fences_untrusted_text(client, monkeypatch) -> None:
    fake = FakeLLM()
    monkeypatch.setattr(runner, "_default_llm", lambda: fake)
    token = await _token(client, await _seed_tenant_with_summarize())

    await _run(client, token, {"text": TEXT})

    assert len(fake.prompts) == 1
    prompt = fake.prompts[0]
    assert prompt.index("<document>") < prompt.index(TEXT) < prompt.index("</document>")


async def test_llm_failure_is_recorded_not_internal(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: FakeLLM(fail=True))
    token = await _token(client, await _seed_tenant_with_summarize())

    body = await _run(client, token, {"text": TEXT})

    assert body["status"] == "failed"
    assert body["error"] == "model call failed"
    assert "internal error" not in body["error"]


async def test_missing_llm_fails_loudly(client, monkeypatch) -> None:
    monkeypatch.setattr(runner, "_default_llm", lambda: None)
    token = await _token(client, await _seed_tenant_with_summarize())

    body = await _run(client, token, {"text": TEXT})

    assert body["status"] == "failed"
    assert "no language model" in body["error"]


async def test_too_short_input_is_rejected_before_any_llm_call(client, monkeypatch) -> None:
    fake = FakeLLM()
    monkeypatch.setattr(runner, "_default_llm", lambda: fake)
    token = await _token(client, await _seed_tenant_with_summarize())

    r = await client.post(
        "/v1/agents/summarize/run",
        json={"text": "too short"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 422
    assert fake.prompts == []
