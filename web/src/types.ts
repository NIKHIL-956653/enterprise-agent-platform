// The API's response shapes, written down once. These mirror the FastAPI response models;
// if the backend changes a field, this is the one file to update.

export type AgentSummary = {
  name: string;
  display_name: string;
  is_enabled: boolean;
  has_implementation: boolean;
};

export type RunEnvelope = {
  run_id: string;
  agent: string;
  status: string;
  output: Record<string, unknown> | null;
  error: string | null;
  latency_ms: number | null;
  prompt_tokens: number;
  completion_tokens: number;
};

export type RunSummary = {
  run_id: string;
  agent: string;
  status: string;
  error: string | null;
  latency_ms: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  created_at: string;
};

export type RunDetail = RunSummary & {
  input: Record<string, unknown>;
  output: Record<string, unknown> | null;
};

export type RunPage = { items: RunSummary[]; next_before: string | null };

export type AuditEvent = {
  seq: number;
  event_type: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
  created_at: string;
  intact: boolean;
};

export type RunAudit = { run_id: string; chain_ok: boolean; events: AuditEvent[] };

export type DocumentSummary = {
  document_id: string;
  title: string;
  external_id: string;
  source: string;
  classification: number;
  chunk_count: number;
  created_at: string;
};

export type DocumentPage = { items: DocumentSummary[]; next_before: string | null };

export type Citation = {
  passage: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  external_id: string;
  ordinal: number;
  similarity: number;
};

export const TERMINAL_STATUSES = new Set(["succeeded", "failed", "timed_out"]);

export const CLASSIFICATION_NAMES = ["public", "internal", "confidential", "restricted"] as const;
