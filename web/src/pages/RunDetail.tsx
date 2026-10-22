// One run, fully accounted for: what went in, what came out, and the audit chain that
// proves nobody rewrote either. The chain banner is the platform's whole pitch in one
// element - the API re-verified every hash in Postgres on this very request, so "verified"
// here is a computation, not a label.

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api";
import type { Citation, RunAudit, RunDetail } from "../types";
import { fmtTime, shortHash } from "../util";

export function RunDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<RunDetail | null>(null);
  const [audit, setAudit] = useState<RunAudit | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    api<RunDetail>(`/v1/runs/${id}`)
      .then(setRun)
      .catch((e: unknown) =>
        setError(
          e instanceof ApiError && e.status === 404
            ? "Run not found."
            : e instanceof Error
              ? e.message
              : String(e),
        ),
      );
    api<RunAudit>(`/v1/runs/${id}/audit`)
      .then(setAudit)
      .catch(() => {});
  }, [id]);

  if (error) return <div className="error">{error}</div>;
  if (!run) return <div className="muted">Loading run…</div>;

  const citations =
    run.agent === "answer" && run.output && Array.isArray(run.output.citations)
      ? (run.output.citations as Citation[])
      : [];

  return (
    <div className="stack">
      <div className="card">
        <h2>
          {run.agent} <span className={`pill ${run.status}`}>{run.status}</span>
        </h2>
        <div className="kv">
          <span>
            started <b>{fmtTime(run.created_at)}</b>
          </span>
          {run.latency_ms !== null && (
            <span>
              latency <b>{run.latency_ms} ms</b>
            </span>
          )}
          <span>
            run id <b className="mono">{run.run_id}</b>
          </span>
        </div>
        {run.error && (
          <div className="error" style={{ marginTop: 10 }}>
            {run.error}
          </div>
        )}
      </div>

      <div className="card">
        <h2>Input</h2>
        <pre>{JSON.stringify(run.input, null, 2)}</pre>
      </div>

      <div className="card">
        <h2>Output</h2>
        {run.output ? <pre>{JSON.stringify(run.output, null, 2)}</pre> : <p className="muted">—</p>}
        {citations.map((c) => (
          <div className="citation" key={c.chunk_id}>
            <b>[{c.passage}]</b> {c.document_title}{" "}
            <span className="muted mono">
              chunk {c.ordinal} · similarity {c.similarity.toFixed(2)}
            </span>
          </div>
        ))}
      </div>

      <div className="card">
        <h2>Audit trail</h2>
        {!audit ? (
          <p className="muted">Loading audit chain…</p>
        ) : (
          <>
            {audit.chain_ok ? (
              <div className="banner ok">
                ✓ Chain verified — every hash recomputed in the database, every link intact.
              </div>
            ) : (
              <div className="banner bad">
                ✗ CHAIN BROKEN — at least one event fails verification. History was edited or an
                event was removed.
              </div>
            )}
            {audit.events.map((e) => (
              <details key={e.seq} className={`event ${e.intact ? "" : "bad"}`}>
                <summary>
                  <span className={`pill ${e.intact ? "succeeded" : "failed"}`}>
                    {e.intact ? "intact" : "broken"}
                  </span>
                  <b>{e.event_type}</b>
                  <span className="muted">seq {e.seq}</span>
                  <span className="muted mono">
                    {shortHash(e.prev_hash)} → {shortHash(e.hash)}
                  </span>
                  <span className="muted">{fmtTime(e.created_at)}</span>
                </summary>
                <pre>{JSON.stringify(e.payload, null, 2)}</pre>
              </details>
            ))}
            {audit.events.length === 0 && <p className="muted">No audit events for this run.</p>}
          </>
        )}
      </div>

      <div>
        <Link to="/runs" className="muted">
          ← back to runs
        </Link>
      </div>
    </div>
  );
}
