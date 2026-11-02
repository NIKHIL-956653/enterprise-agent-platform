"""
Per-tenant daily token budget (EAP-64).

EAP-46's limiter counts REQUESTS per minute - a throttle for availability. This counts
TOKENS per day - a cap on spend. They differ in three ways that shape this module:

- The unit is what the provider bills: prompt_tokens + completion_tokens, as the model
  reported them, summed over the tenant's agent_runs. No Redis counter: the runs table is
  the ledger, and a ledger that disagrees with the invoice is not one. The query hits the
  (tenant_id, created_at DESC) index for one day of rows.
- The day is the TENANT'S day. "Today" is a question about where the students are, so the
  window is a calendar day in settings.token_budget_timezone (Asia/Kolkata for the pilot);
  a UTC midnight would reset the budget in the middle of an afternoon class.
- It fails CLOSED. The request limiter fails open when Redis is down because it protects
  availability; this protects a bill. If the database cannot answer "how much today", the
  request fails - and the database being unreachable fails the request anyway.

Two call sites, same check: the run endpoint refuses with a 429 before anything is queued,
and the runner refuses before each model call inside a run, so a long multi-call run or a
queue of runs that all passed the endpoint cannot overshoot by more than one call.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.config import get_settings
from eap.core.deps import get_current_tenant_id, get_tenant_session


@dataclass(frozen=True)
class BudgetStatus:
    # The tenant's daily_token_budget. 0 means no cap. None means the session saw no tenant
    # row at all - no tenant context - which reads as exhausted, not as unlimited: the
    # closed direction, like every other default in this codebase.
    limit: int | None
    used: int
    day_start: datetime
    reset_at: datetime  # the next local midnight, as an aware datetime

    @property
    def capped(self) -> bool:
        return self.limit is None or self.limit > 0

    @property
    def exhausted(self) -> bool:
        return self.limit is None or (self.limit > 0 and self.used >= self.limit)

    @property
    def remaining(self) -> int:
        if self.limit is None:
            return 0
        return max(self.limit - self.used, 0) if self.limit > 0 else 0


class BudgetExhausted(Exception):
    """Today's tokens are spent. str() is the message a student may read."""

    def __init__(self, budget: BudgetStatus) -> None:
        self.budget = budget
        super().__init__(reset_message(budget))


def zone() -> ZoneInfo:
    return ZoneInfo(get_settings().token_budget_timezone)


def day_window(now: datetime | None = None, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """(start of today, start of tomorrow) in the budget zone, as aware datetimes.

    Computed in local time and converted, not by adding 24h to a UTC instant: a zone with
    daylight saving has 23- and 25-hour days, and the budget day must follow the calendar.
    """
    tz = tz or zone()
    local_now = (now or datetime.now(UTC)).astimezone(tz)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Aware + timedelta is wall-clock arithmetic in Python, so this is the next local
    # midnight even across a DST change; ZoneInfo supplies that midnight's offset.
    return start, start + timedelta(days=1)


def reset_message(budget: BudgetStatus, now: datetime | None = None) -> str:
    """Plain words, no jargon: when, in the students' own time, and how long that is."""
    now = now or datetime.now(UTC)
    reset = budget.reset_at
    seconds = max(int((reset - now).total_seconds()), 0)
    hours, minutes = divmod(seconds // 60, 60)
    wait = f"{hours} h {minutes} min" if hours else f"{minutes} min"
    stamp = reset.strftime("%H:%M %Z")
    if budget.limit is None:
        return "No token budget is visible for this tenant, so no model call can be made."
    return (
        f"This college has used its token budget for today ({budget.used:,} of {budget.limit:,}). "
        f"It resets at {stamp} (in about {wait}). Please try again then."
    )


def retry_after_seconds(budget: BudgetStatus, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    return max(int((budget.reset_at - now).total_seconds()), 1)


async def status_for(session: AsyncSession, now: datetime | None = None) -> BudgetStatus:
    """
    This tenant's budget and today's spend, read through an RLS-scoped session.

    Neither query names a tenant. The session's app.tenant_id shows the caller exactly one
    tenants row and only their own runs - the same mechanism every other read relies on.
    """
    day_start, reset_at = day_window(now)
    limit = (await session.execute(text("SELECT daily_token_budget FROM tenants"))).scalar_one_or_none()
    used = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                "FROM agent_runs WHERE created_at >= :day_start"
            ),
            {"day_start": day_start},
        )
    ).scalar_one()
    return BudgetStatus(
        limit=int(limit) if limit is not None else None,
        used=int(used),
        day_start=day_start,
        reset_at=reset_at,
    )


async def ensure_within_budget(session: AsyncSession, now: datetime | None = None) -> BudgetStatus:
    """The check both call sites use. Raises BudgetExhausted; returns the status otherwise."""
    budget = await status_for(session, now)
    if budget.exhausted:
        raise BudgetExhausted(budget)
    return budget


async def enforce_daily_token_budget(
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> None:
    """
    FastAPI dependency for the run endpoint: 429 once today's tokens are gone.

    It refuses EVERY run, not only ones that will call a model - the endpoint cannot know
    which agent will, and "this college is out of tokens today" is a simpler truth to tell
    a student than "this particular agent would have been free". Retry-After says when.
    """
    try:
        budget = await ensure_within_budget(session)
    except BudgetExhausted as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
            headers={
                "Retry-After": str(retry_after_seconds(e.budget)),
                "X-TokenBudget-Limit": str(e.budget.limit or 0),
                "X-TokenBudget-Used": str(e.budget.used),
            },
        ) from e
    # tenant_id is a dependency only so the token is verified before the query runs; the
    # query itself is scoped by the session, never by this value.
    del tenant_id, budget
