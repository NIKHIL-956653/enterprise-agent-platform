"""
Ops summary for a tenant's own admin (EAP-65).

GET /v1/ops/summary?days=7   runs per day, p50/p95 latency, refusal and error rates,
                             tokens, thumbs up/down - this tenant only

Admins and owners only, by the role claim in the token. A member gets 403: this is not
about hiding data that exists elsewhere (there is nothing tenant-specific in the URL), it
is a role boundary, so the honest answer is "not for you".

Scoping is the session's, as everywhere: no query names a tenant, RLS shows the caller
their own rows. There is deliberately no "all tenants" variant of this endpoint - the
platform owner's cross-tenant view is scripts/ops_report.py against the database, never a
token with extra powers. See eap.ops.summary.
"""

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.deps import get_claims, get_tenant_session
from eap.models.user import UserRole
from eap.ops import MAX_DAYS, OpsSummary, summarize

router = APIRouter(prefix="/ops", tags=["ops"])

_ADMIN_ROLES = {UserRole.ADMIN.value, UserRole.OWNER.value}


async def require_admin(claims: Annotated[dict[str, Any], Depends(get_claims)]) -> None:
    if claims.get("role") not in _ADMIN_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admins only")


class DayCountOut(BaseModel):
    day: date
    runs: int


class FeedbackOut(BaseModel):
    up: int
    down: int
    # up / (up + down); null until anyone has voted.
    ratio: float | None


class OpsSummaryOut(BaseModel):
    days: int
    timezone: str
    since: datetime
    runs: int
    per_day: list[DayCountOut]
    p50_latency_ms: int | None
    p95_latency_ms: int | None
    answer_runs: int
    refusals: int
    # refusals / answer_runs: how often the corpus did not cover the question. Null with no
    # answer runs. High is not "bad" by itself - it says the students ask about things the
    # college has not uploaded, which is the most useful thing this page can tell an admin.
    refusal_rate: float | None
    errors: int
    error_rate: float | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    feedback: FeedbackOut


def to_out(s: OpsSummary) -> OpsSummaryOut:
    return OpsSummaryOut(
        days=s.days,
        timezone=s.timezone,
        since=s.since,
        runs=s.runs,
        per_day=[DayCountOut(day=d.day, runs=d.runs) for d in s.per_day],
        p50_latency_ms=s.p50_latency_ms,
        p95_latency_ms=s.p95_latency_ms,
        answer_runs=s.answer_runs,
        refusals=s.refusals,
        refusal_rate=s.refusal_rate,
        errors=s.errors,
        error_rate=s.error_rate,
        prompt_tokens=s.prompt_tokens,
        completion_tokens=s.completion_tokens,
        total_tokens=s.total_tokens,
        feedback=FeedbackOut(up=s.feedback.up, down=s.feedback.down, ratio=s.feedback.ratio),
    )


@router.get("/summary", response_model=OpsSummaryOut, dependencies=[Depends(require_admin)])
async def ops_summary(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = 7,
) -> OpsSummaryOut:
    return to_out(await summarize(session, days=days))
