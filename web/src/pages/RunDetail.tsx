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
              <div key={e.seq} className={`event ${e.intact ? "" : "bad"}`}>
                <div className="event-head">
                  <span className={`pill ${e.intact ? "succeeded" : "failed"}`}>
                    {e.intact ? "intact" : "broken"}
                  </span>
                  <b>{e.event_type}</b>
                  <span className="muted">seq {e.seq}</span>
                  <span className="muted mono">
                    {shortHash(e.prev_hash)} → {shortHash(e.hash)}
                  </span>
                  <span className="muted">{fmtTime(e.created_at)}</span>
                </div>
                <PayloadSummary type={e.event_type} payload={e.payload} />
              </div>
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

// ---------------------------------------------------------------- audit payloads
//
// Rendered as key/value lines, always on screen (D3). They used to sit inside a collapsed
// <details> whose disclosure marker the CSS removed, so nothing said the JSON was one click
// away - and a closed <details> is not rendered, so select-all + copy skipped it too. The
// payload was never empty; it was never on screen.

const PREVIEW_CHARS = 200;

// The keys each event type leads with, in display order. Anything else in the payload is
// appended generically, because an audit view that silently drops a key is one that lies.
const LEAD_KEYS: Record<string, string[]> = {
  "run.started": ["agent", "input"],
  "llm.call": ["prompt", "completion", "max_output_tokens"],
  "llm.error": ["prompt"],
  "retrieval.performed": ["query", "top_k", "min_similarity", "chunks"],
  "run.finished": ["status", "latency_ms", "error"],
};

// Keys left out of one event because the page already shows them twice (header and
// run.started). Not information loss - just not a third copy.
const SKIP_KEYS: Record<string, string[]> = {
  "run.finished": ["agent"],
};

const LABELS: Record<string, string> = {
  max_output_tokens: "max output tokens",
  top_k: "top-k",
  min_similarity: "similarity floor",
  latency_ms: "latency",
  _redactions: "redacted values",
};

type Row = { key: string; label: string; value: string };

function payloadRows(type: string, payload: Record<string, unknown>): Row[] {
  const lead = LEAD_KEYS[type] ?? [];
  const skip = SKIP_KEYS[type] ?? [];
  const keys = [
    ...lead.filter((k) => k in payload),
    ...Object.keys(payload).filter((k) => !lead.includes(k) && !skip.includes(k)),
  ];
  const out: Row[] = [];
  for (const key of keys) {
    const v = payload[key];
    if (v === null || v === undefined) continue; // e.g. `error: null` on a clean run
    if (key === "input" && v && typeof v === "object" && !Array.isArray(v)) {
      for (const [k, iv] of Object.entries(v as Record<string, unknown>)) {
        out.push({ key: `input.${k}`, label: `input · ${k}`, value: display(iv) });
      }
      continue;
    }
    if (key === "latency_ms" && typeof v === "number") {
      out.push({ key, label: LABELS[key], value: `${v} ms` });
      continue;
    }
    if (key === "chunks" && Array.isArray(v)) {
      out.push({ key, label: "chunks", value: chunksText(v) });
      continue;
    }
    out.push({ key, label: LABELS[key] ?? key.replace(/_/g, " "), value: display(v) });
  }
  return out;
}

function display(v: unknown): string {
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v); // a nested shape with no better rendering: compact, and clamped below
}

function chunksText(chunks: unknown[]): string {
  if (chunks.length === 0) return "none cleared the similarity floor";
  const parts = chunks.map((c) => {
    const r = c as { ordinal?: unknown; similarity?: unknown };
    const sim = typeof r.similarity === "number" ? r.similarity.toFixed(2) : "?";
    return `chunk ${String(r.ordinal ?? "?")} · similarity ${sim}`;
  });
  return `${chunks.length} retrieved: ${parts.join("; ")}`;
}

function PayloadSummary({
  type,
  payload,
}: {
  type: string;
  payload: Record<string, unknown> | null | undefined;
}) {
  const list = payload ? payloadRows(type, payload) : [];
  if (list.length === 0) return null; // genuinely nothing to show: no block at all
  return (
    <dl className="payload">
      {list.map((r) => (
        <div key={r.key} className="payload-row">
          <dt>{r.label}</dt>
          <dd>
            <Clamp text={r.value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Clamp({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (text.length <= PREVIEW_CHARS) return <>{text}</>;
  return (
    <>
      {open ? text : text.slice(0, PREVIEW_CHARS).trimEnd() + "…"}{" "}
      <button type="button" className="linkish" onClick={() => setOpen((o) => !o)}>
        {open ? "show less" : `show more (${text.length.toLocaleString()} characters)`}
      </button>
    </>
  );
}
