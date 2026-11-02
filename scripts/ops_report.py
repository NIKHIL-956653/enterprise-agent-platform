"""
The platform operator's view: every tenant's pilot numbers, side by side (EAP-65).

    python scripts/ops_report.py                # last 7 days, all tenants
    python scripts/ops_report.py --days 30
    python scripts/ops_report.py --tenant engg --tenant mba

Runs against the database as the OWNER role (DATABASE_URL), the same connection migrations
and seeds use, and prints the same aggregates GET /v1/ops/summary gives a tenant's admin -
runs per day, p50/p95 latency, refusal and error rates, tokens, thumbs - for all tenants in
one table.

This split is deliberate, and it is the reason this file exists at all. The app has no
cross-tenant endpoint and must never grow one: every request to the API runs as eap_app
under RLS and sees exactly one tenant, and no token - admin, owner, or anything added later -
can ask it for another tenant's numbers. The one party entitled to see all tenants is the
platform operator, and the operator already holds the owner credentials. So the cross-tenant
view goes through this script, against the database, with those credentials, on the
operator's machine; never through the app, where a bug or a stolen token could reach it.
If you find yourself wanting an "all tenants" flag on the API, the answer is this script.

The owner role bypasses RLS, so here the scoping is the explicit tenant_id passed to
eap.ops.summarize for each tenant. The function is the same one the endpoint calls; what
differs is who is asking and how the tenant is named.

Days follow TOKEN_BUDGET_TIMEZONE (Asia/Kolkata by default): "7 days" is today and the six
before it in that calendar, the same window the Ops tab shows.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from eap.core.db import dispose_owner_engine, get_owner_session_factory
from eap.ops import MAX_DAYS, OpsSummary, summarize


@dataclass(frozen=True)
class TenantRow:
    slug: str
    name: str
    daily_token_budget: int
    summary: OpsSummary


async def collect(days: int, slugs: tuple[str, ...] | None = None) -> list[TenantRow]:
    """One OpsSummary per tenant, oldest tenant first. Owner session; explicit tenant scope."""
    rows: list[TenantRow] = []
    async with get_owner_session_factory()() as s:
        sql = "SELECT id, slug, name, daily_token_budget FROM tenants"
        params: dict[str, object] = {}
        if slugs:
            sql += " WHERE slug = ANY(:slugs)"
            params["slugs"] = list(slugs)
        tenants = (await s.execute(text(sql + " ORDER BY created_at, slug"), params)).all()
        for t in tenants:
            summary = await summarize(s, days=days, tenant_id=UUID(str(t.id)))
            rows.append(
                TenantRow(slug=t.slug, name=t.name, daily_token_budget=t.daily_token_budget, summary=summary)
            )
    return rows


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _ms(value: int | None) -> str:
    return "-" if value is None else f"{value} ms"


def render(rows: list[TenantRow], days: int) -> str:
    """Tenants as columns, metrics as rows - the shape a person compares colleges in."""
    if not rows:
        return "no tenants"
    label_width = 22
    col = max(12, *(len(r.slug) for r in rows)) + 2

    def line(label: str, cells: list[str]) -> str:
        return f"{label:<{label_width}}" + "".join(f"{c:>{col}}" for c in cells)

    out = [
        f"ops report - last {days} day(s) ending today in {rows[0].summary.timezone}",
        "",
        line("tenant", [r.slug for r in rows]),
        line("", ["-" * (col - 2) for _ in rows]),
        line("runs", [str(r.summary.runs) for r in rows]),
        line("errors (rate)", [f"{r.summary.errors} ({_pct(r.summary.error_rate)})" for r in rows]),
        line("answer runs", [str(r.summary.answer_runs) for r in rows]),
        line("refusals (rate)", [f"{r.summary.refusals} ({_pct(r.summary.refusal_rate)})" for r in rows]),
        line("p50 latency", [_ms(r.summary.p50_latency_ms) for r in rows]),
        line("p95 latency", [_ms(r.summary.p95_latency_ms) for r in rows]),
        line("tokens", [f"{r.summary.total_tokens:,}" for r in rows]),
        line("  prompt", [f"{r.summary.prompt_tokens:,}" for r in rows]),
        line("  completion", [f"{r.summary.completion_tokens:,}" for r in rows]),
        line("daily budget", [f"{r.daily_token_budget:,}" if r.daily_token_budget else "none" for r in rows]),
        line("thumbs up / down", [f"{r.summary.feedback.up} / {r.summary.feedback.down}" for r in rows]),
        line("thumbs up share", [_pct(r.summary.feedback.ratio) for r in rows]),
        "",
        line("runs per day", ["" for _ in rows]),
    ]
    for i, day in enumerate(rows[0].summary.per_day):
        out.append(line(f"  {day.day.isoformat()}", [str(r.summary.per_day[i].runs) for r in rows]))
    return "\n".join(out)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    p.add_argument(
        "--days", type=int, default=7, help=f"calendar days ending today (1-{MAX_DAYS}); default 7"
    )
    p.add_argument("--tenant", action="append", dest="tenants", metavar="SLUG", help="limit to these slugs")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.days <= MAX_DAYS:
        print(f"--days must be between 1 and {MAX_DAYS}", file=sys.stderr)
        return 2

    async def _go() -> list[TenantRow]:
        try:
            return await collect(args.days, tuple(args.tenants) if args.tenants else None)
        finally:
            await dispose_owner_engine()

    print(render(asyncio.run(_go()), args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
