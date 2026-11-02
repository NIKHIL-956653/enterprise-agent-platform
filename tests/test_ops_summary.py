"""
Ops summary (EAP-65): GET /v1/ops/summary for a tenant's admin, scripts/ops_report.py for
the operator.

The same aggregates, two callers. The endpoint tests prove an admin sees their own tenant's
numbers and nothing else, through RLS with no tenant filter in the code; the script tests
prove the operator sees every tenant, side by side, through the owner role and an explicit
tenant id. One fixture builds a known little history - echo runs, a summarize run, a
failed run, answer refusals, two thumbs - so every rate has a number to check against.
"""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from scripts import ops_report
from sqlalchemy import text

from eap.agents import runner
from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.llm.base import Completion, LLMError
from eap.ops import summarize, window_since
from eap.rag import ingest
from tests.test_retrieval import RankingFakeEmbedder

pytestmark = pytest.mark.usefixtures("temporal_worker")

PASSWORD = "correct-horse-battery-staple"
IST = ZoneInfo("Asia/Kolkata")
TEXT = (
    "The platform runs many agents for many tenants. Each tenant's rows are isolated by "
    "row-level security, and every agent run is recorded whether it succeeds or fails."
)


class MeteredFake:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def complete(self, prompt: str, *, max_output_tokens: int = 1024) -> str:
        return (await self.complete_metered(prompt, max_output_tokens=max_output_tokens)).text

    async def complete_metered(self, prompt: str, *, max_output_tokens: int = 1024) -> Completion:
        if self.fail:
            raise LLMError("model call failed")
        return Completion("A canned summary.", 100, 25)


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


async def _seed_tenant() -> dict:
    """One tenant: an admin, a member, every agent the fixture below runs."""
    slug = f"t-{uuid.uuid4().hex[:8]}"
    admin, member = f"admin-{uuid.uuid4().hex[:6]}@example.com", f"stu-{uuid.uuid4().hex[:6]}@example.com"
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
        for email, role in ((admin, "admin"), (member, "member")):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, role, clearance) "
                    "VALUES (gen_random_uuid(), :tid, :email, :ph, :role, 1)"
                ),
                {"tid": tid, "email": email, "ph": hash_password(PASSWORD), "role": role},
            )
        for name in ("echo", "summarize", "answer"):
            await s.execute(
                text(
                    "INSERT INTO agents (id, tenant_id, name, display_name) "
                    "VALUES (gen_random_uuid(), :tid, :name, :name)"
                ),
                {"tid": tid, "name": name},
            )
        await s.commit()
    return {"slug": slug, "admin": admin, "member": member, "tenant_id": str(tid)}


async def _headers(client: AsyncClient, seed: dict, who: str) -> dict[str, str]:
    r = await client.post(
        "/v1/auth/login", json={"tenant_slug": seed["slug"], "email": seed[who], "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _run(client: AsyncClient, h: dict[str, str], agent: str, payload: dict) -> dict:
    r = await client.post(f"/v1/agents/{agent}/run", json=payload, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _history(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> dict:
    """
    A tenant with: 3 echo runs, 1 summarize (100+25 tokens), 1 failed summarize, 2 answer
    runs that both refuse (empty corpus -> no model call), one thumb up and one down.
    So: 7 runs, 1 error, 2 answer runs, 2 refusals, 125 tokens, 1/1 thumbs.
    """
    seed = await _seed_tenant()
    h = await _headers(client, seed, "member")
    good, bad = MeteredFake(), MeteredFake(fail=True)
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: good)
    for i in range(3):
        await _run(client, h, "echo", {"message": str(i)})
    first = await _run(client, h, "summarize", {"text": TEXT})
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: bad)
    failed = await _run(client, h, "summarize", {"text": TEXT})
    assert failed["status"] == "failed"
    monkeypatch.setattr(runner, "_default_llm", lambda _slug: good)
    refusal = await _run(client, h, "answer", {"question": "Where is marina parking?"})
    assert refusal["output"]["answered"] is False
    await _run(client, h, "answer", {"question": "Where is the pool?"})

    r = await client.post(f"/v1/runs/{first['run_id']}/feedback", json={"verdict": "up"}, headers=h)
    assert r.status_code == 200
    r = await client.post(f"/v1/runs/{refusal['run_id']}/feedback", json={"verdict": "down"}, headers=h)
    assert r.status_code == 200
    return seed


# --- the endpoint -------------------------------------------------------------------------


async def test_an_admin_gets_their_tenants_numbers(client: AsyncClient, monkeypatch) -> None:
    seed = await _history(client, monkeypatch)

    r = await client.get("/v1/ops/summary", params={"days": 7}, headers=await _headers(client, seed, "admin"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 7
    assert body["timezone"] == "Asia/Kolkata"
    assert body["runs"] == 7
    assert body["errors"] == 1
    assert body["error_rate"] == pytest.approx(1 / 7)
    assert (body["answer_runs"], body["refusals"], body["refusal_rate"]) == (2, 2, 1.0)
    assert (body["prompt_tokens"], body["completion_tokens"], body["total_tokens"]) == (100, 25, 125)
    assert body["feedback"] == {"up": 1, "down": 1, "ratio": 0.5}
    assert body["p50_latency_ms"] is not None and body["p95_latency_ms"] >= body["p50_latency_ms"]
    # Every day of the window is present, oldest first, and today holds all seven runs.
    assert len(body["per_day"]) == 7
    assert [d["runs"] for d in body["per_day"]] == [0, 0, 0, 0, 0, 0, 7]
    assert body["per_day"][-1]["day"] == datetime.now(IST).date().isoformat()


async def test_a_member_is_403(client: AsyncClient) -> None:
    seed = await _seed_tenant()
    r = await client.get("/v1/ops/summary", headers=await _headers(client, seed, "member"))
    assert r.status_code == 403


async def test_no_token_is_401(client: AsyncClient) -> None:
    assert (await client.get("/v1/ops/summary")).status_code == 401


@pytest.mark.parametrize("days", [0, 91, "x"])
async def test_days_is_bounded(client: AsyncClient, days) -> None:
    seed = await _seed_tenant()
    r = await client.get(
        "/v1/ops/summary", params={"days": days}, headers=await _headers(client, seed, "admin")
    )
    assert r.status_code == 422


async def test_another_tenants_admin_sees_only_their_own_empty_tenant(
    client: AsyncClient, monkeypatch
) -> None:
    """RLS, with no tenant filter in eap.ops.summary for this path - the one that matters."""
    await _history(client, monkeypatch)
    other = await _seed_tenant()

    r = await client.get("/v1/ops/summary", headers=await _headers(client, other, "admin"))

    assert r.status_code == 200
    body = r.json()
    assert body["runs"] == 0 and body["errors"] == 0 and body["total_tokens"] == 0
    assert body["feedback"] == {"up": 0, "down": 0, "ratio": None}
    assert body["refusal_rate"] is None and body["error_rate"] is None
    assert body["p50_latency_ms"] is None
    assert [d["runs"] for d in body["per_day"]] == [0] * 7


async def test_runs_before_the_window_are_left_out(client: AsyncClient, monkeypatch) -> None:
    seed = await _history(client, monkeypatch)
    since, _ = window_since(7, tz=IST)
    async with get_owner_session_factory()() as s:
        # Push the three echo runs to just before the window; everything else stays today.
        await s.execute(
            text(
                "UPDATE agent_runs SET created_at = :when WHERE tenant_id = :tid AND agent_id = "
                "(SELECT id FROM agents WHERE tenant_id = :tid AND name = 'echo')"
            ),
            {"when": since - timedelta(minutes=1), "tid": seed["tenant_id"]},
        )
        await s.commit()

    r = await client.get("/v1/ops/summary", params={"days": 7}, headers=await _headers(client, seed, "admin"))
    assert r.json()["runs"] == 4
    r = await client.get("/v1/ops/summary", params={"days": 8}, headers=await _headers(client, seed, "admin"))
    assert r.json()["runs"] == 7


def test_the_window_is_calendar_days_in_kolkata() -> None:
    # 20:00 UTC Nov 2 is 01:30 IST Nov 3. Seven days: Oct 28 .. Nov 3, starting 18:30 UTC Oct 27.
    since, dates = window_since(7, datetime(2026, 11, 2, 20, 0, tzinfo=UTC), IST)
    assert since == datetime(2026, 10, 27, 18, 30, tzinfo=UTC)
    assert [d.isoformat() for d in dates] == [
        "2026-10-28",
        "2026-10-29",
        "2026-10-30",
        "2026-10-31",
        "2026-11-01",
        "2026-11-02",
        "2026-11-03",
    ]


async def test_a_run_late_last_night_ist_lands_on_yesterdays_bar(client: AsyncClient, monkeypatch) -> None:
    seed = await _history(client, monkeypatch)
    since, dates = window_since(7, tz=IST)
    today_start = since + timedelta(days=6)
    async with get_owner_session_factory()() as s:
        await s.execute(
            text("UPDATE agent_runs SET created_at = :when WHERE tenant_id = :tid"),
            {"when": today_start - timedelta(minutes=5), "tid": seed["tenant_id"]},
        )
        await s.commit()

    body = (await client.get("/v1/ops/summary", headers=await _headers(client, seed, "admin"))).json()
    assert [d["runs"] for d in body["per_day"]] == [0, 0, 0, 0, 0, 7, 0]
    assert body["per_day"][5]["day"] == dates[5].isoformat()


# --- the operator's script ----------------------------------------------------------------


async def test_the_operator_sees_every_tenant_side_by_side(client: AsyncClient, monkeypatch) -> None:
    busy = await _history(client, monkeypatch)
    quiet = await _seed_tenant()

    rows = await ops_report.collect(7, (busy["slug"], quiet["slug"]))

    assert [r.slug for r in rows] == [busy["slug"], quiet["slug"]]
    assert rows[0].summary.runs == 7 and rows[0].summary.total_tokens == 125
    assert rows[0].summary.feedback.up == 1 and rows[0].summary.feedback.down == 1
    assert rows[1].summary.runs == 0 and rows[1].summary.feedback.ratio is None

    report = ops_report.render(rows, 7)
    header = report.splitlines()[2]
    assert busy["slug"] in header and quiet["slug"] in header
    assert "refusals (rate)" in report and "2 (100%)" in report
    assert "thumbs up / down" in report and "1 / 1" in report
    assert "daily budget" in report
    assert len([ln for ln in report.splitlines() if ln.startswith("  20")]) == 7  # one line per day


async def test_the_script_matches_the_endpoint_for_the_same_tenant(client: AsyncClient, monkeypatch) -> None:
    seed = await _history(client, monkeypatch)
    api = (await client.get("/v1/ops/summary", headers=await _headers(client, seed, "admin"))).json()
    (row,) = await ops_report.collect(7, (seed["slug"],))
    s = row.summary
    assert (s.runs, s.errors, s.answer_runs, s.refusals, s.total_tokens) == (
        api["runs"],
        api["errors"],
        api["answer_runs"],
        api["refusals"],
        api["total_tokens"],
    )
    assert [d.runs for d in s.per_day] == [d["runs"] for d in api["per_day"]]
    assert (s.p50_latency_ms, s.p95_latency_ms) == (api["p50_latency_ms"], api["p95_latency_ms"])


async def test_the_app_has_no_cross_tenant_route(client: AsyncClient) -> None:
    """The split is a property, not a convention: the API offers no way to name a tenant."""
    from eap.main import app

    ops_paths = [path for path in app.openapi()["paths"] if path.startswith("/v1/ops")]
    assert ops_paths == ["/v1/ops/summary"]
    seed = await _seed_tenant()
    h = await _headers(client, seed, "admin")
    for params in ({"tenant": "engg"}, {"tenant_id": seed["tenant_id"]}, {"all": "true"}):
        body = (await client.get("/v1/ops/summary", params=params, headers=h)).json()
        assert body["runs"] == 0  # unknown parameters change nothing; the session scopes


async def test_summarize_rejects_a_bad_window() -> None:
    async with get_owner_session_factory()() as s:
        with pytest.raises(ValueError):
            await summarize(s, days=0, tenant_id=uuid.uuid4())
