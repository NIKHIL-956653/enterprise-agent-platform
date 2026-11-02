// How the pilot is going, for this tenant's admin: one small table of aggregates and one
// bar per day. Everything on this page comes from GET /v1/ops/summary, which the API scopes
// to the caller's tenant by RLS - there is no tenant picker here because there is nothing to
// pick. The platform operator's cross-tenant view is scripts/ops_report.py, on purpose.

import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { OpsSummary } from "../types";

const WINDOWS = [7, 14, 30] as const;

function pct(v: number | null): string {
  return v === null ? "—" : `${Math.round(v * 100)}%`;
}

function ms(v: number | null): string {
  return v === null ? "—" : `${v} ms`;
}

export function Ops() {
  const [days, setDays] = useState<(typeof WINDOWS)[number]>(7);
  const [data, setData] = useState<OpsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    api<OpsSummary>(`/v1/ops/summary?days=${days}`)
      .then(setData)
      .catch((e: unknown) =>
        setError(
          e instanceof ApiError && e.status === 403
            ? "This tab is for tenant admins."
            : e instanceof Error
              ? e.message
              : String(e),
        ),
      );
  }, [days]);

  const max = data ? Math.max(1, ...data.per_day.map((d) => d.runs)) : 1;

  return (
    <div className="stack">
      <div className="toolbar">
        <label>
          Window
          <select value={days} onChange={(e) => setDays(Number(e.target.value) as (typeof WINDOWS)[number])}>
            {WINDOWS.map((w) => (
              <option key={w} value={w}>
                last {w} days
              </option>
            ))}
          </select>
        </label>
        {data && (
          <span className="muted" style={{ alignSelf: "center" }}>
            calendar days in {data.timezone}; today is partial
          </span>
        )}
      </div>

      {error && <div className="error">{error}</div>}
      {!data && !error && <div className="muted">Loading…</div>}

      {data && (
        <>
          <div className="card">
            <h2>Runs per day</h2>
            <div className="bars" role="img" aria-label="Runs per day">
              {data.per_day.map((d) => (
                <div className="bar-col" key={d.day} title={`${d.day}: ${d.runs} run(s)`}>
                  <div className="bar-count muted">{d.runs || ""}</div>
                  <div className="bar" style={{ height: `${Math.round((d.runs / max) * 100)}%` }} />
                  <div className="bar-label muted">{d.day.slice(5)}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <h2>Last {data.days} days</h2>
            <table className="ops">
              <tbody>
                <tr>
                  <th>Runs</th>
                  <td>{data.runs}</td>
                </tr>
                <tr>
                  <th>Errors (failed or timed out)</th>
                  <td>
                    {data.errors} <span className="muted">({pct(data.error_rate)})</span>
                  </td>
                </tr>
                <tr>
                  <th>Latency p50 / p95</th>
                  <td>
                    {ms(data.p50_latency_ms)} / {ms(data.p95_latency_ms)}
                  </td>
                </tr>
                <tr>
                  <th>Answer runs</th>
                  <td>{data.answer_runs}</td>
                </tr>
                <tr>
                  <th>
                    Refusals <span className="muted">(not covered by the documents)</span>
                  </th>
                  <td>
                    {data.refusals} <span className="muted">({pct(data.refusal_rate)} of answer runs)</span>
                  </td>
                </tr>
                <tr>
                  <th>Tokens (prompt + completion)</th>
                  <td>
                    {data.total_tokens.toLocaleString()}{" "}
                    <span className="muted">
                      ({data.prompt_tokens.toLocaleString()} + {data.completion_tokens.toLocaleString()})
                    </span>
                  </td>
                </tr>
                <tr>
                  <th>Thumbs up / down</th>
                  <td>
                    {data.feedback.up} / {data.feedback.down}{" "}
                    <span className="muted">({pct(data.feedback.ratio)} up)</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <p className="hint" style={{ marginTop: 10 }}>
              A high refusal rate is not an error: it means students ask about things that are not in
              the uploaded documents yet.
            </p>
          </div>
        </>
      )}
    </div>
  );
}
