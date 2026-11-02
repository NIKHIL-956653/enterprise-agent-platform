"""
Per-tenant daily token budget (EAP-64), and the token accounting it stands on.

Before this ticket prompt_tokens and completion_tokens on agent_runs were always 0: the LLM
boundary returned text alone. So the first tests here prove the counts are real - the
provider's numbers when it reports them, an estimate when it does not - and the rest prove
the cap: counted per calendar day in Asia/Kolkata, checked before each model call, a 429
with the reset time at the endpoint, a FAILED run with the same words inside the worker.

Fakes stand in for the model, as everywhere else. One reports usage (MeteredFake), one
only knows complete() (PlainFake); both must be counted.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from eap.agents import runner
from eap.agents.runner import execute_agent
from eap.core import budget
from eap.core.config import get_settings
from eap.core.db import get_owner_session_factory, get_session_factory
from eap.core.security import hash_password
from eap.llm import anthropic as anthropic_llm
from eap.llm import gemini as gemini_llm
from eap.llm.base import Completion, estimate_tokens
from eap.models.tenant import DEFAULT_DAILY_TOKEN_BUDGET

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "correct-horse-battery-staple"
IST = ZoneInfo("Asia/Kolkata")
TEXT = (
    "The platform runs many agents for many tenants. Each tenant's rows are isolated by "
    "row-level security, and every agent run is recorded whether it succeeds or fails."
)


class MeteredFake:
    """Reports usage the way a real provider does."""

    def __init__(self, prompt_tokens: int = 120, completion_tokens: int = 30) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.calls = 0

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        return (await self.complete_metered(prompt, max_output_tokens=max_output_tokens)).text

    async def complete_metered(self, prompt: str, *, max_output_tokens: int = 1024) -> Completion:
        self.calls += 1
        return Completion("A canned summary.", self.prompt_tokens, self.completion_tokens)


class PlainFake:
    """Only knows complete(): the shape every older test fake has."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        self.calls += 1
        return "A canned summary."


async def _seed_tenant(daily_token_budget: int | None = None) -> dict:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    async with get_owner_session_factory()() as s:
        if daily_token_budget is None:
            sql = "INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :slug, :slug) RETURNING id"
        else:
            sql = (
                "INSERT INTO tenants (id, slug, name, daily_token_budget) "
                "VALUES (gen_random_uuid(), :slug, :slug, :budget) RETURNING id"
            )
        tid = (await s.execute(text(sql), {"slug": slug, "budget": daily_token_budget})).scalar_one()
        uid = (
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash) "
                    "VALUES (gen_random_uuid(), :tid, :email, :ph) RETURNING id"
                ),
                {"tid": tid, "email": email, "ph": hash_password(PASSWORD)},
            )
        ).scalar_one()
        for name in ("summarize", "echo"):
            await s.execute(
                text(
                    "INSERT INTO agents (id, tenant_id, name, display_name) "
                    "VALUES (gen_random_uuid(), :tid, :name, :name)"
                ),
                {"tid": tid, "name": name},
            )
        await s.commit()
    return {"slug": slug, "email": email, "tenant_id": str(tid), "user_id": str(uid)}


async def _headers(client: AsyncClient, seed: dict) -> dict[str, str]:
    r = await client.post(
        "/v1/auth/login", json={"tenant_slug": seed["slug"], "email": seed["email"], "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _summarize(client: AsyncClient, headers: dict[str, str]):
    return await client.post("/v1/agents/summarize/run", json={"text": TEXT}, headers=headers)


async def _backdate_runs(tenant_id: str, when: datetime) -> None:
    async with get_owner_session_factory()() as s:
        await s.execute(
            text("UPDATE agent_runs SET created_at = :when WHERE tenant_id = :tid"),
            {"when": when, "tid": tenant_id},
        )
        await s.commit()


# --- accounting ---------------------------------------------------------------------------


async def test_a_metered_model_puts_its_own_counts_on_the_run(client: AsyncClient, monkeypatch) -> None:
    fake = MeteredFake(prompt_tokens=120, completion_tokens=30)
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    seed = await _seed_tenant()
    h = await _headers(client, seed)

    r = await _summarize(client, h)

    assert r.status_code == 200, r.text
    assert (r.json()["prompt_tokens"], r.json()["completion_tokens"]) == (120, 30)
    detail = await client.get(f"/v1/runs/{r.json()['run_id']}", headers=h)
    assert (detail.json()["prompt_tokens"], detail.json()["completion_tokens"]) == (120, 30)

    audit = await client.get(f"/v1/runs/{r.json()['run_id']}/audit", headers=h)
    call = next(e for e in audit.json()["events"] if e["event_type"] == "llm.call")
    assert (call["payload"]["prompt_tokens"], call["payload"]["completion_tokens"]) == (120, 30)
    assert call["payload"]["usage"] == "reported"


async def test_an_unmetered_model_is_estimated_never_zero(client: AsyncClient, monkeypatch) -> None:
    fake = PlainFake()
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    seed = await _seed_tenant()
    h = await _headers(client, seed)

    r = await _summarize(client, h)

    assert r.status_code == 200, r.text
    assert r.json()["prompt_tokens"] > estimate_tokens(TEXT)  # the prompt wraps the text
    assert r.json()["completion_tokens"] == estimate_tokens("A canned summary.")
    audit = await client.get(f"/v1/runs/{r.json()['run_id']}/audit", headers=h)
    call = next(e for e in audit.json()["events"] if e["event_type"] == "llm.call")
    assert call["payload"]["usage"] == "estimated"


def test_estimate_is_four_chars_per_token_rounded_up() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_gemini_usage_counts_thinking_as_output_and_survives_a_missing_block() -> None:
    usage = SimpleNamespace(prompt_token_count=100, candidates_token_count=40, thoughts_token_count=15)
    assert gemini_llm.gemini_usage(SimpleNamespace(usage_metadata=usage)) == (100, 55)
    assert gemini_llm.gemini_usage(SimpleNamespace(usage_metadata=None)) == (0, 0)
    assert gemini_llm.gemini_usage(SimpleNamespace()) == (0, 0)


def test_anthropic_usage_counts_cache_tokens_as_input() -> None:
    usage = SimpleNamespace(
        input_tokens=50, output_tokens=20, cache_creation_input_tokens=30, cache_read_input_tokens=None
    )
    assert anthropic_llm.anthropic_usage(SimpleNamespace(usage=usage)) == (80, 20)
    assert anthropic_llm.anthropic_usage(SimpleNamespace(usage=None)) == (0, 0)


# --- the day --------------------------------------------------------------------------------


def test_the_budget_day_is_a_calendar_day_in_kolkata() -> None:
    # 20:00 UTC on Nov 2 is 01:30 IST on Nov 3: the day started at 18:30 UTC on Nov 2.
    start, reset = budget.day_window(datetime(2026, 11, 2, 20, 0, tzinfo=UTC), IST)
    assert start == datetime(2026, 11, 2, 18, 30, tzinfo=UTC)
    assert reset == datetime(2026, 11, 3, 18, 30, tzinfo=UTC)
    assert start.astimezone(IST).hour == 0 and reset.astimezone(IST).strftime("%H:%M") == "00:00"
    # And 18:00 UTC the same evening is still Nov 2 in Kolkata: a different day start.
    start2, _ = budget.day_window(datetime(2026, 11, 2, 18, 0, tzinfo=UTC), IST)
    assert start2 == datetime(2026, 11, 1, 18, 30, tzinfo=UTC)


def test_the_zone_comes_from_settings(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_BUDGET_TIMEZONE", "Asia/Dubai")
    get_settings.cache_clear()
    start, _ = budget.day_window(datetime(2026, 11, 2, 21, 0, tzinfo=UTC))
    assert start == datetime(2026, 11, 2, 20, 0, tzinfo=UTC)  # 00:00 GST on Nov 3


def test_the_message_says_when_it_resets_in_plain_words() -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # 17:30 IST
    start, reset = budget.day_window(now, IST)
    msg = budget.reset_message(
        budget.BudgetStatus(limit=1000, used=1200, day_start=start, reset_at=reset), now
    )
    assert msg == (
        "This college has used its token budget for today (1,200 of 1,000). "
        "It resets at 00:00 IST (in about 6 h 30 min). Please try again then."
    )
    assert (
        budget.retry_after_seconds(
            budget.BudgetStatus(limit=1000, used=1200, day_start=start, reset_at=reset), now
        )
        == 6 * 3600 + 30 * 60
    )


# --- the cap --------------------------------------------------------------------------------


async def test_new_tenants_get_the_default_budget_and_the_check_refuses_negative() -> None:
    seed = await _seed_tenant()
    async with get_owner_session_factory()() as s:
        got = (
            await s.execute(
                text("SELECT daily_token_budget FROM tenants WHERE id = :tid"), {"tid": seed["tenant_id"]}
            )
        ).scalar_one()
        assert got == DEFAULT_DAILY_TOKEN_BUDGET
        with pytest.raises(Exception, match="ck_tenants_daily_token_budget"):
            await s.execute(
                text("UPDATE tenants SET daily_token_budget = -1 WHERE id = :tid"), {"tid": seed["tenant_id"]}
            )


async def test_the_endpoint_answers_429_with_the_reset_time_once_the_budget_is_spent(
    client: AsyncClient, monkeypatch
) -> None:
    fake = MeteredFake(prompt_tokens=120, completion_tokens=30)
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    seed = await _seed_tenant(daily_token_budget=200)
    h = await _headers(client, seed)

    first = await _summarize(client, h)  # used 0 < 200: allowed, costs 150
    assert first.status_code == 200, first.text
    second = await _summarize(client, h)  # used 150 < 200: allowed, costs 150 -> 300
    assert second.status_code == 200, second.text

    third = await _summarize(client, h)  # used 300 >= 200: refused before anything is queued
    assert third.status_code == 429, third.text
    assert "resets at 00:00 IST" in third.json()["detail"]
    assert "300 of 200" in third.json()["detail"]
    assert int(third.headers["Retry-After"]) > 0
    assert third.headers["X-TokenBudget-Used"] == "300"
    assert fake.calls == 2

    # Nothing was recorded for the refused request.
    runs = await client.get("/v1/runs", headers=h)
    assert len(runs.json()["items"]) == 2

    # Echo never calls a model, and is refused all the same: out of tokens means out.
    echo = await client.post("/v1/agents/echo/run", json={"message": "hi"}, headers=h)
    assert echo.status_code == 429


async def test_yesterdays_spend_does_not_count(client: AsyncClient, monkeypatch) -> None:
    fake = MeteredFake(prompt_tokens=120, completion_tokens=30)
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    seed = await _seed_tenant(daily_token_budget=200)
    h = await _headers(client, seed)
    assert (await _summarize(client, h)).status_code == 200
    assert (await _summarize(client, h)).status_code == 200
    assert (await _summarize(client, h)).status_code == 429

    # Move both runs to 23:59 IST yesterday. The window starts at 00:00 IST today, so
    # they fall out of it and the budget is fresh.
    day_start, _ = budget.day_window(tz=IST)
    await _backdate_runs(seed["tenant_id"], day_start - timedelta(minutes=1))

    assert (await _summarize(client, h)).status_code == 200


async def test_a_run_at_one_minute_past_midnight_counts_toward_the_new_day(
    client: AsyncClient, monkeypatch
) -> None:
    fake = MeteredFake(prompt_tokens=120, completion_tokens=30)
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    seed = await _seed_tenant(daily_token_budget=200)
    h = await _headers(client, seed)
    assert (await _summarize(client, h)).status_code == 200
    assert (await _summarize(client, h)).status_code == 200

    day_start, _ = budget.day_window(tz=IST)
    await _backdate_runs(seed["tenant_id"], day_start + timedelta(minutes=1))

    assert (await _summarize(client, h)).status_code == 429


async def test_zero_means_no_cap(client: AsyncClient, monkeypatch) -> None:
    fake = MeteredFake(prompt_tokens=10_000, completion_tokens=10_000)
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    seed = await _seed_tenant(daily_token_budget=0)
    h = await _headers(client, seed)
    for _ in range(3):
        assert (await _summarize(client, h)).status_code == 200


async def test_one_tenants_spend_is_not_anothers(client: AsyncClient, monkeypatch) -> None:
    fake = MeteredFake(prompt_tokens=120, completion_tokens=30)
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: fake)
    spender, other = await _seed_tenant(daily_token_budget=100), await _seed_tenant(daily_token_budget=100)
    hs, ho = await _headers(client, spender), await _headers(client, other)
    assert (await _summarize(client, hs)).status_code == 200
    assert (await _summarize(client, hs)).status_code == 429
    assert (await _summarize(client, ho)).status_code == 200


async def test_the_worker_refuses_before_the_model_call_when_the_budget_is_gone(monkeypatch) -> None:
    """
    The endpoint check is what a student sees. This is the one underneath it: a run that is
    already executing - queued earlier, or one of several calls in one run - asks again
    before every model call, and gets a FAILED run with the same words, not a model call.
    """
    seed = await _seed_tenant(daily_token_budget=100)
    fake = MeteredFake()
    # Spend the budget by hand: a finished run from earlier today.
    async with get_owner_session_factory()() as s:
        aid = (
            await s.execute(
                text("SELECT id FROM agents WHERE tenant_id = :tid AND name = 'summarize'"),
                {"tid": seed["tenant_id"]},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO agent_runs "
                "  (id, tenant_id, agent_id, status, input, prompt_tokens, completion_tokens) "
                "VALUES (gen_random_uuid(), :tid, :aid, 'succeeded', '{}', 90, 20)"
            ),
            {"tid": seed["tenant_id"], "aid": aid},
        )
        await s.commit()

    async with get_session_factory()() as s:
        await s.execute(
            text("SELECT set_config('app.tenant_id', :tid, true), set_config('app.clearance', '3', true)"),
            {"tid": seed["tenant_id"]},
        )
        run = await execute_agent(
            session=s,
            tenant_id=uuid.UUID(seed["tenant_id"]),
            user_id=uuid.UUID(seed["user_id"]),
            agent_name="summarize",
            payload={"text": TEXT},
            llm=fake,
        )
        await s.commit()

    assert run.status.value == "failed"
    assert run.error is not None and "resets at 00:00 IST" in run.error
    assert "110 of 100" in run.error
    assert fake.calls == 0
    assert (run.prompt_tokens, run.completion_tokens) == (0, 0)


async def test_a_session_with_no_tenant_context_reads_as_exhausted() -> None:
    """Fail closed: no visible tenant row is not 'unlimited'."""
    async with get_session_factory()() as s:
        status = await budget.status_for(s)
    assert status.limit is None
    assert status.exhausted
