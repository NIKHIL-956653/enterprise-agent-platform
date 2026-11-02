"""
The pilot's numbers: runs per day, latency percentiles, refusal and error rates, tokens,
and the thumbs - over the last N calendar days in the tenant's zone.

One function computes them, and it is called from two places with two kinds of session,
on purpose:

- `GET /v1/ops/summary` hands it an RLS-scoped session and NO tenant filter. The caller's
  own tenant is all the session can see, so the aggregates are theirs by construction, the
  same way every other read in this codebase is.
- `scripts/ops_report.py` hands it the OWNER session and an explicit `tenant_id`, once per
  tenant, and lays the results side by side. The owner role bypasses RLS, so the filter is
  the whole scoping there - which is exactly why the cross-tenant view is a script the
  platform operator runs against the database, and not an endpoint of the app. No token,
  however privileged, can ask the API for another tenant's numbers.

Days follow settings.token_budget_timezone (Asia/Kolkata for the pilot), the same calendar
the token budget uses, so "today" means the same thing on the Ops tab and in a 429.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core import budget

MAX_DAYS = 90


@dataclass(frozen=True)
class DayCount:
    day: date
    runs: int


@dataclass(frozen=True)
class FeedbackCounts:
    up: int
    down: int

    @property
    def ratio(self) -> float | None:
        """Share of thumbs that were up. None until someone has voted - 0/0 is not 0."""
        total = self.up + self.down
        return self.up / total if total else None


@dataclass(frozen=True)
class OpsSummary:
    days: int
    timezone: str
    since: datetime  # start of the first day in the window, UTC
    runs: int
    per_day: list[DayCount]
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    answer_runs: int  # succeeded runs of the `answer` agent
    refusals: int  # ...of which answered=false
    errors: int  # failed + timed_out
    prompt_tokens: int
    completion_tokens: int
    feedback: FeedbackCounts

    @property
    def refusal_rate(self) -> float | None:
        return self.refusals / self.answer_runs if self.answer_runs else None

    @property
    def error_rate(self) -> float | None:
        return self.errors / self.runs if self.runs else None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def window_since(
    days: int, now: datetime | None = None, tz: ZoneInfo | None = None
) -> tuple[datetime, list[date]]:
    """
    (UTC instant the window starts, the local dates it covers, oldest first).

    "7 days" is today and the six before it, as calendar days in the tenant's zone - so a
    report pulled at 09:00 shows today's nine hours as a partial day rather than a 24-hour
    slice ending now, which is how people read "per day".
    """
    tz = tz or budget.zone()
    today_start, _ = budget.day_window(now, tz)
    first = today_start - timedelta(days=days - 1)
    return first.astimezone(UTC), [(first + timedelta(days=i)).date() for i in range(days)]


# Each statement takes the window start and, only when the caller is the operator, a tenant
# id. The RLS path leaves the clause out entirely: there is no tenant to name, and naming
# one would be the application doing what the database already does.
def _scope(tenant_id: UUID | None, alias: str) -> str:
    return f" AND {alias}.tenant_id = :tid" if tenant_id is not None else ""


async def summarize(
    session: AsyncSession,
    *,
    days: int,
    tenant_id: UUID | None = None,
    now: datetime | None = None,
) -> OpsSummary:
    """Aggregates for the last `days` calendar days. See the module docstring for scoping."""
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    tz_name = budget.zone().key
    since, dates = window_since(days, now)
    params: dict[str, object] = {"since": since, "tz": tz_name}
    if tenant_id is not None:
        params["tid"] = tenant_id

    totals = (
        await session.execute(
            text(
                f"""
                SELECT count(*)                                                      AS runs,
                       percentile_cont(0.5)  WITHIN GROUP (ORDER BY r.latency_ms)    AS p50,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY r.latency_ms)    AS p95,
                       count(*) FILTER (WHERE r.status IN ('failed', 'timed_out'))   AS errors,
                       count(*) FILTER (WHERE a.name = 'answer' AND r.status = 'succeeded') AS answer_runs,
                       count(*) FILTER (WHERE a.name = 'answer' AND r.status = 'succeeded'
                                          AND r.output->>'answered' = 'false')        AS refusals,
                       COALESCE(SUM(r.prompt_tokens), 0)                             AS prompt_tokens,
                       COALESCE(SUM(r.completion_tokens), 0)                         AS completion_tokens
                  FROM agent_runs r
                  JOIN agents a ON a.id = r.agent_id
                 WHERE r.created_at >= :since{_scope(tenant_id, "r")}
                """
            ),
            params,
        )
    ).one()

    # The day is the LOCAL date: created_at AT TIME ZONE :tz yields the wall-clock time in
    # that zone, and ::date keeps its calendar day. Days with no runs come back as 0 below,
    # so a chart has every bar and a quiet weekend reads as quiet, not missing.
    per_day_rows = (
        await session.execute(
            text(
                f"""
                SELECT (r.created_at AT TIME ZONE :tz)::date AS day, count(*) AS runs
                  FROM agent_runs r
                 WHERE r.created_at >= :since{_scope(tenant_id, "r")}
                 GROUP BY 1
                """
            ),
            params,
        )
    ).all()
    counts = {row.day: int(row.runs) for row in per_day_rows}

    votes = (
        await session.execute(
            text(
                f"""
                SELECT f.verdict, count(*) AS n
                  FROM feedback f
                 WHERE f.created_at >= :since{_scope(tenant_id, "f")}
                 GROUP BY f.verdict
                """
            ),
            params,
        )
    ).all()
    by_verdict = {row.verdict: int(row.n) for row in votes}

    return OpsSummary(
        days=days,
        timezone=tz_name,
        since=since,
        runs=int(totals.runs),
        per_day=[DayCount(day=d, runs=counts.get(d, 0)) for d in dates],
        p50_latency_ms=int(round(totals.p50)) if totals.p50 is not None else None,
        p95_latency_ms=int(round(totals.p95)) if totals.p95 is not None else None,
        answer_runs=int(totals.answer_runs),
        refusals=int(totals.refusals),
        errors=int(totals.errors),
        prompt_tokens=int(totals.prompt_tokens),
        completion_tokens=int(totals.completion_tokens),
        feedback=FeedbackCounts(up=by_verdict.get("up", 0), down=by_verdict.get("down", 0)),
    )
