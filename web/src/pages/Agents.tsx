// The run console: pick an agent, give it typed input, watch the run finish.
//
// Submission is async-then-poll on purpose. A real model call can take 30 seconds, and a
// browser tab holding one HTTP request open that long looks frozen. So the POST uses
// ?async=true (the API answers "queued" immediately with a run id) and the page polls the
// run until it reaches a terminal status - the same contract any API client of the
// platform would use, which means this screen doubles as living documentation.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import {
  TERMINAL_STATUSES,
  type AgentSummary,
  type Citation,
  type RunDetail,
  type RunEnvelope,
} from "../types";
import { AgentForm, splitServerErrors, type FieldErrors } from "../components/AgentForm";

export function Agents() {
  const [agents, setAgents] = useState<AgentSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<AgentSummary[]>("/v1/agents")
      .then((list) => {
        setAgents(list);
        setSelected((s) => s ?? list.find((a) => a.is_enabled)?.name ?? null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) return <div className="error">{error}</div>;
  if (!agents) return <div className="muted">Loading agents…</div>;

  const current = agents.find((a) => a.name === selected) ?? null;

  return (
    <div className="stack">
      <div className="agent-list">
        {agents.map((a) => (
          <button
            key={a.name}
            className={`agent-chip ${a.name === selected ? "sel" : ""}`}
            onClick={() => setSelected(a.name)}
          >
            {a.display_name}
            {!a.has_implementation && (
              <span className="warnbadge" title="Enabled in the database but no code serves it">
                {" "}
                ⚠ no implementation
              </span>
            )}
          </button>
        ))}
      </div>
      {current && <RunPanel key={current.name} agent={current} />}
    </div>
  );
}

// ---------------------------------------------------------------- run panel

function RunPanel({ agent }: { agent: AgentSummary }) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [result, setResult] = useState<RunDetail | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [fieldErrs, setFieldErrs] = useState<FieldErrors>({});
  const fieldNames = Object.keys(agent.input_schema?.properties ?? {});
  // One idempotency key per *submission*: a retry of the same failed submission reuses it
  // (the platform then returns the same run instead of starting a second one); it rotates
  // only after a run actually completes.
  const keyRef = useRef<string>(crypto.randomUUID());

  async function run(payload: Record<string, unknown>) {
    setBusy(true);
    setErrors([]);
    setFieldErrs({});
    setResult(null);
    setStatus("queued");
    try {
      const env = await api<RunEnvelope>(`/v1/agents/${agent.name}/run?async=true`, {
        method: "POST",
        body: payload,
        headers: { "Idempotency-Key": keyRef.current },
      });
      const deadline = Date.now() + 120_000;
      let last: RunDetail | null = null;
      while (Date.now() < deadline) {
        last = await api<RunDetail>(`/v1/runs/${env.run_id}`);
        setStatus(last.status);
        if (TERMINAL_STATUSES.has(last.status)) break;
        await new Promise((r) => setTimeout(r, 700));
      }
      setResult(last);
      keyRef.current = crypto.randomUUID();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const { byField, rest } = splitServerErrors(e.detail, fieldNames);
        setFieldErrs(byField);
        setErrors(rest);
      } else if (e instanceof ApiError && e.status === 429)
        setErrors(["Rate limit reached for this tenant. Wait a minute and retry."]);
      else setErrors([e instanceof Error ? e.message : String(e)]);
      setStatus(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h2>
        {agent.display_name} {status && <span className={`pill ${status}`}>{status}</span>}
      </h2>
      {agent.input_schema ? (
        <AgentForm
          agent={agent.name}
          schema={agent.input_schema}
          busy={busy}
          serverErrors={fieldErrs}
          onRun={run}
        />
      ) : (
        <p className="muted">No code serves this agent on this server, so it cannot be run.</p>
      )}
      {errors.length > 0 && (
        <div className="error" style={{ marginTop: 12 }}>
          {errors.map((e, i) => (
            <div key={i}>{e}</div>
          ))}
        </div>
      )}
      {result && <RunResult run={result} />}
    </div>
  );
}

// ---------------------------------------------------------------- results

function RunResult({ run }: { run: RunDetail }) {
  return (
    <div style={{ marginTop: 16 }}>
      {run.status === "succeeded" ? (
        <AgentOutput run={run} />
      ) : (
        <div className="error">
          Run {run.status}
          {run.error ? `: ${run.error}` : ""}
        </div>
      )}
      <div className="kv" style={{ marginTop: 10 }}>
        {run.latency_ms !== null && (
          <span>
            latency <b>{run.latency_ms} ms</b>
          </span>
        )}
        <span>
          run{" "}
          <Link className="mono" to={`/runs/${run.run_id}`}>
            {run.run_id.slice(0, 8)}… → full detail & audit chain
          </Link>
        </span>
      </div>
    </div>
  );
}

function AgentOutput({ run }: { run: RunDetail }): ReactNode {
  const out = run.output ?? {};

  if (run.agent === "summarize" && typeof out.summary === "string") {
    return (
      <div className="card inner">
        <h2>Summary</h2>
        <p>{out.summary}</p>
      </div>
    );
  }

  if (run.agent === "answer" && typeof out.answer === "string") {
    const citations = Array.isArray(out.citations) ? (out.citations as Citation[]) : [];
    return (
      <div className="card inner">
        <h2>{out.answered === false ? "Refused" : "Answer"}</h2>
        <p>{out.answer}</p>
        {citations.map((c) => (
          <div className="citation" key={c.chunk_id}>
            <b>[{c.passage}]</b> {c.document_title}{" "}
            <span className="muted mono">
              chunk {c.ordinal} · similarity {c.similarity.toFixed(2)}
            </span>
          </div>
        ))}
        {out.answered === false && (
          <p className="muted">
            Nothing in this tenant's documents cleared the similarity floor, so the platform
            refused instead of inventing an answer.
          </p>
        )}
      </div>
    );
  }

  if (run.agent === "docwriter" && typeof out.document === "string") {
    return (
      <div className="card inner">
        <h2>
          Document{" "}
          <span className={`pill ${out.approved ? "succeeded" : "failed"}`}>
            {out.approved ? "approved" : "not approved"}
          </span>{" "}
          <span className="muted">after {String(out.revisions ?? 0)} revision(s)</span>
        </h2>
        <pre>{out.document}</pre>
      </div>
    );
  }

  return (
    <div className="card inner">
      <h2>Output</h2>
      <pre>{JSON.stringify(out, null, 2)}</pre>
    </div>
  );
}
