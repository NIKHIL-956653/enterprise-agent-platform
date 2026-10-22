// The tenant's document library: what the answer agent can ground on. The classification
// column is the clearance story made visible - and what this list does NOT show is as
// deliberate as what it does: no bodies (a browse list is not a bulk export), and no rows
// above the caller's clearance, because for this session those rows do not exist.

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { CLASSIFICATION_NAMES, type DocumentPage, type DocumentSummary } from "../types";
import { fieldErrors, fmtTime } from "../util";

export function Documents() {
  const [items, setItems] = useState<DocumentSummary[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (before: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: "20" });
      if (before) params.set("before", before);
      const page = await api<DocumentPage>(`/v1/documents?${params}`);
      setItems((prev) => (before ? [...prev, ...page.items] : page.items));
      setNextBefore(page.next_before);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load(null);
  }, [load]);

  return (
    <div className="stack">
      <UploadForm onDone={() => void load(null)} />
      {error && <div className="error">{error}</div>}
      <div className="card">
        <h2>Documents</h2>
        <table>
          <thead>
            <tr>
              <th>Title</th>
              <th>External id</th>
              <th>Classification</th>
              <th>Chunks</th>
              <th>Added</th>
            </tr>
          </thead>
          <tbody>
            {items.map((d) => (
              <tr key={d.document_id}>
                <td>{d.title}</td>
                <td className="mono muted">{d.external_id}</td>
                <td>
                  <span className={`pill c${d.classification}`}>
                    {CLASSIFICATION_NAMES[d.classification] ?? d.classification}
                  </span>
                </td>
                <td className="muted">{d.chunk_count}</td>
                <td className="muted">{fmtTime(d.created_at)}</td>
              </tr>
            ))}
            {items.length === 0 && !busy && (
              <tr>
                <td colSpan={5} className="muted">
                  No documents visible at your clearance. Upload one above.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {nextBefore && (
          <div style={{ marginTop: 12 }}>
            <button className="ghost" disabled={busy} onClick={() => void load(nextBefore)}>
              {busy ? "Loading…" : "Load older documents"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function UploadForm({ onDone }: { onDone: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [classification, setClassification] = useState(0);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErrors([]);
    setOkMsg(null);
    try {
      const res = await api<{ document_id: string; chunk_count: number }>("/v1/documents", {
        method: "POST",
        body: { title, body, classification },
      });
      setOkMsg(`Ingested: ${res.chunk_count} chunk(s) embedded and stored.`);
      setTitle("");
      setBody("");
      onDone();
    } catch (err) {
      if (err instanceof ApiError && err.status === 403)
        setErrors(["That classification is above your clearance - the database refused the row."]);
      else if (err instanceof ApiError && err.status === 503)
        setErrors(["Embeddings are unavailable right now (provider down or no key). Try again."]);
      else if (err instanceof ApiError && err.status === 422) setErrors(fieldErrors(err.detail));
      else setErrors([err instanceof Error ? err.message : String(err)]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card panel" onSubmit={onSubmit}>
      <h2>Add a document</h2>
      <div className="grid2">
        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required />
        </label>
        <label>
          Classification
          <select
            value={classification}
            onChange={(e) => setClassification(Number(e.target.value))}
          >
            {CLASSIFICATION_NAMES.map((name, i) => (
              <option key={name} value={i}>
                {i} — {name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        Body (plain text; it will be chunked and embedded)
        <textarea rows={8} value={body} onChange={(e) => setBody(e.target.value)} required />
      </label>
      {errors.map((e, i) => (
        <div className="error" key={i}>
          {e}
        </div>
      ))}
      {okMsg && <div className="banner ok">{okMsg}</div>}
      <div>
        <button disabled={busy}>{busy ? "Ingesting…" : "Ingest document"}</button>
      </div>
    </form>
  );
}
