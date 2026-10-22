// The run console: pick an agent, give it typed input, watch the run finish.
//
// Submission is async-then-poll on purpose. A real model call can take 30 seconds, and a
// browser tab holding one HTTP request open that long looks frozen. So the POST uses
// ?async=true (the API answers "queued" immediately with a run id) and the page polls the
// run until it reaches a terminal status - the same contract any API client of the
// platform would use, which means this screen doubles as living documentation.

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api";
import {
  TERMINAL_STATUSES,
  type AgentSummary,
  type Citation,
  type RunDetail,
  type RunEnvelope,
} from "../types";
import { fieldErrors } from "../util";

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
  // One idempotency key per *submission*: a retry of the same failed submission reuses it
  // (the platform then returns the same run instead of starting a second one); it rotates
  // only after a run actually completes.
  const keyRef = useRef<string>(crypto.randomUUID());

  async function run(payload: Record<string, unknown>) {
    setBusy(true);
    setErrors([]);
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
      if (e instanceof ApiError && e.status === 422) setErrors(fieldErrors(e.detail));
      else if (e instanceof ApiError && e.status === 429)
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
      <AgentForm name={agent.name} busy={busy} onRun={run} />
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

function AgentForm({
  name,
  busy,
  onRun,
}: {
  name: string;
  busy: boolean;
  onRun: (p: Record<string, unknown>) => void;
}) {
  if (name === "summarize") return <SummarizeForm busy={busy} onRun={onRun} />;
  if (name === "answer") return <AnswerForm busy={busy} onRun={onRun} />;
  if (name === "docwriter") return <DocwriterForm busy={busy} onRun={onRun} />;
  return <RawForm busy={busy} onRun={onRun} />;
}

type FormProps = { busy: boolean; onRun: (p: Record<string, unknown>) => void };

function SummarizeForm({ busy, onRun }: FormProps) {
  const [text, setText] = useState("");
  const [maxSentences, setMaxSentences] = useState(3);
  return (
    <form
      className="panel"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        onRun({ text, max_sentences: maxSentences });
      }}
    >
      <label>
        Text to summarize (50–20,000 characters)
        <textarea rows={8} value={text} onChange={(e) => setText(e.target.value)} required />
      </label>
      <div className="grid2">
        <label>
          Max sentences
          <input
            type="number"
            min={1}
            max={10}
            value={maxSentences}
            onChange={(e) => setMaxSentences(Number(e.target.value))}
          />
        </label>
      </div>
      <div>
        <button disabled={busy}>{busy ? "Running…" : "Run summarize"}</button>
      </div>
    </form>
  );
}

function AnswerForm({ busy, onRun }: FormProps) {
  const [question, setQuestion] = useState("");
  const [topK, setTopK] = useState(5);
  return (
    <form
      className="panel"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        onRun({ question, top_k: topK });
      }}
    >
      <label>
        Question (answered only from this tenant's documents)
        <input value={question} onChange={(e) => setQuestion(e.target.value)} required />
      </label>
      <div className="grid2">
        <label>
          Passages to retrieve (top-k)
          <input
            type="number"
            min={1}
            max={20}
            value={topK}
            onChange={(e) => setTopK(Number(e.target.value))}
          />
        </label>
      </div>
      <div>
        <button disabled={busy}>{busy ? "Running…" : "Ask"}</button>
      </div>
    </form>
  );
}

function DocwriterForm({ busy, onRun }: FormProps) {
  const [ticketId, setTicketId] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  return (
    <form
      className="panel"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        onRun({ ticket_id: ticketId, title, description });
      }}
    >
      <div className="grid2">
        <label>
          Ticket id
          <input value={ticketId} onChange={(e) => setTicketId(e.target.value)} required />
        </label>
        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required />
        </label>
      </div>
      <label>
        Description
        <textarea
          rows={6}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          required
        />
      </label>
      <div>
        <button disabled={busy}>{busy ? "Running…" : "Write document"}</button>
      </div>
    </form>
  );
}

function RawForm({ busy, onRun }: FormProps) {
  // Agents without a dedicated form (echo, and whatever ships next) still get a console:
  // raw JSON in, validated by the API's own 422, which the panel renders field by field.
  const [raw, setRaw] = useState("{}");
  const [parseError, setParseError] = useState<string | null>(null);
  return (
    <form
      className="panel"
      onSubmit={(e: FormEvent) => {
        e.preventDefault();
        try {
          const parsed: unknown = JSON.parse(raw);
          setParseError(null);
          onRun(parsed as Record<string, unknown>);
        } catch {
          setParseError("Not valid JSON.");
        }
      }}
    >
      <label>
        Payload (JSON)
        <textarea className="mono" rows={6} value={raw} onChange={(e) => setRaw(e.target.value)} />
      </label>
      {parseError && <div className="error">{parseError}</div>}
      <div>
        <button disabled={busy}>{busy ? "Running…" : "Run"}</button>
      </div>
    </form>
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
