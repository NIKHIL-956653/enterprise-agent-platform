// Run history. Keyset pagination exactly as the API offers it: "everything older than the
// last row you saw". There are deliberately no page numbers - the backend cannot jump to
// page 7 (that is what makes page 500 cost what page 1 costs), and a numbered pager here
// would be a lie about what the system underneath can do.

import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { AgentSummary, RunPage, RunSummary } from "../types";
import { fmtTime } from "../util";

const STATUSES = ["", "queued", "running", "succeeded", "failed", "timed_out"];

export function Runs() {
  const [items, setItems] = useState<RunSummary[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agent, setAgent] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const load = useCallback(
    async (before: string | null) => {
      setBusy(true);
      setError(null);
      try {
        const params = new URLSearchParams({ limit: "20" });
        if (agent) params.set("agent", agent);
        if (status) params.set("status", status);
        if (before) params.set("before", before);
        const page = await api<RunPage>(`/v1/runs?${params}`);
        setItems((prev) => (before ? [...prev, ...page.items] : page.items));
        setNextBefore(page.next_before);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [agent, status],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  useEffect(() => {
    api<AgentSummary[]>("/v1/agents")
      .then(setAgents)
      .catch(() => {});
  }, []);

  return (
    <div className="stack">
      <div className="toolbar">
        <label>
          Agent
          <select value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">all</option>
            {agents.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Status
          <select value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s || "all"}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Agent</th>
              <th>Status</th>
              <th>Latency</th>
              <th>Run</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.run_id} className="click" onClick={() => navigate(`/runs/${r.run_id}`)}>
                <td className="muted">{fmtTime(r.created_at)}</td>
                <td>{r.agent}</td>
                <td>
                  <span className={`pill ${r.status}`}>{r.status}</span>
                </td>
                <td className="muted">{r.latency_ms !== null ? `${r.latency_ms} ms` : "—"}</td>
                <td className="mono muted">{r.run_id.slice(0, 8)}…</td>
              </tr>
            ))}
            {items.length === 0 && !busy && (
              <tr>
                <td colSpan={5} className="muted">
                  No runs yet. Run an agent from the Agents tab.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {nextBefore && (
          <div style={{ marginTop: 12 }}>
            <button className="ghost" disabled={busy} onClick={() => void load(nextBefore)}>
              {busy ? "Loading…" : "Load older runs"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
