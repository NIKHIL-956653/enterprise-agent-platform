# Backlog

Every commit references a ticket id. Tickets are closed by a merged PR, not by being "done".

Status: `✅ done` · `🔨 in progress` · `⬜ todo` · `⏸️ deferred`

Last updated: 2026-09-29, end of M2.

---

## Sprint 0 — M0 Skeleton ✅

| ID | Title | Status |
|---|---|---|
| EAP-1 | Project scaffold — src layout, `pyproject.toml`, ruff + pytest config, `.gitignore` | ✅ |
| EAP-2 | Core services — settings, async engine, Redis pool, structlog, request-id middleware | ✅ |
| EAP-3 | Health API — `/livez`, `/readyz`, app factory with lifespan | ✅ |
| EAP-4 | Local stack — docker-compose (pgvector + Redis), Dockerfile, dev script | ✅ |
| EAP-5 | Alembic — async env, naming convention, `0001` baseline enabling pgvector | ✅ |
| EAP-6 | Test suite + CI — integration tests, GitHub Actions running lint/migrate/test | ✅ |
| EAP-7 | Repo hygiene + README — `.gitattributes`, LF normalisation, README, this backlog | ✅ |
| EAP-8 | PRD + ADR-001 (stack) | ✅ |
| EAP-9 | `/readyz` returned 200 while degraded → now 503 | ✅ |
| EAP-10 | Health checks could starve the connection pool → 5s probe cache | ✅ |

EAP-9 and EAP-10 were bugs found during M0 and fixed during M2.

---

## Sprint 1 — M1 Tenants + Auth ✅

| ID | Title | Status |
|---|---|---|
| EAP-11 | `tenants` table + migration `0002` | ✅ |
| EAP-12 | `users` scoped to a tenant, argon2id hashing, RLS policies (`0003`) | ✅ |
| EAP-13 | JWT issue + verify; `/v1/auth/login`, `/v1/auth/me` | ✅ |
| EAP-14 | `get_current_tenant` — `tenant_id` from the token, never the request body | ✅ |
| EAP-15 | Tenant isolation at the query layer + a test proving A cannot read B | ✅ |
| EAP-16 | Tenant CRUD API for admins | ⏸️ |

**EAP-15 was the one that mattered.** Multi-tenancy is a security property, not a column.
ADR-002 records the decision: pooled tenancy, RLS + FORCE, slug lookup through a
SECURITY DEFINER function (`0004`), app connecting as the restricted role `eap_app` (`0005`).

---

## Sprint 2 — M2 Agent Runtime ✅

| ID | Title | Status |
|---|---|---|
| EAP-17 | Agent base — Protocol, typed input/output, `AgentContext` | ✅ |
| EAP-18 | Agent registry — name → implementation, fails fast at dispatch | ✅ |
| EAP-19 | Gemini summarize agent + the `LLM` Protocol boundary | ✅ |
| EAP-20 | `POST /v1/agents/{name}/run` — an `agent_runs` row per execution | ✅ |
| EAP-21 | Doc-writer agent on LangGraph — outline/draft/review loop | ✅ |
| EAP-22 | Run history — `GET /v1/runs` (keyset `?before=`), `GET /v1/runs/{id}` | ✅ |
| EAP-23 | App connects as `eap_app`, so every request is subject to RLS | ✅ |
| EAP-24 | CI hardening — `ruff format`, `alembic check`, tests run as `eap_app` | ✅ |

Closed 2026-09-29: 6 migrations, 40 tests green on main.

---

## Deferred

### EAP-16 — Tenant CRUD API for admins ⏸️
**Type:** feature · **Deferred:** end of M1

Creating a tenant is inherently cross-tenant, so it cannot run as `eap_app` — it needs the
owner session and a platform-admin role that does not exist yet. `scripts/seed_demo.py`
stands in: one tenant, one owner, the registered agents.

**Revisit when:** a second real tenant is needed, or before M5 — the admin UI has nowhere
to POST without it.

---

## Small backlog

| ID | Title | Why it waits |
|---|---|---|
| EAP-25 | LLM as a FastAPI dependency (`dependency_overrides` instead of monkeypatch) | Works today; the fix is cosmetic until a second injected service exists |
| EAP-26 | Live Jira/Confluence integration for docwriter | Needs per-tenant secrets — a milestone of its own, not a ticket |
| EAP-37 | Composite `(created_at, id)` pagination cursor | Timestamp-only cursor can duplicate one row on a boundary tie |
| EAP-38 | `eap_app` password out of migration `0005` | Hardcoded, public, unrotatable. Fine for a throwaway local DB, nowhere else |
| EAP-39 | `seed_demo.py` prints a hardcoded agent list | Will drift the next time an agent is added; one-line fix |

---

## Sprint 3 — M3 RAG per tenant 🔨

Each tenant gets its own document corpus; agents answer from it and cite what they used.
Same shape as M1 — the feature is easy, the isolation is the point.

| ID | Title | Status |
|---|---|---|
| EAP-27 | docs: BACKLOG catch-up (M0–M2) + M3 sprint plan | 🔨 |
| EAP-28 | Embeddings boundary — `Embedder` Protocol, `GeminiEmbedder`, fake | ⬜ |
| EAP-29 | `documents` + `document_chunks`, migration `0007`, RLS + FORCE | ⬜ |
| EAP-30 | Chunking — pure function, overlap, boundary tests | ⬜ |
| EAP-31 | `POST /v1/documents` — chunk, batch-embed, store | ⬜ |
| EAP-32 | Retrieval — cosine top-k scoped to the tenant | ⬜ |
| EAP-33 | ADR-003 — vector index choice and RAG design | ⬜ |
| EAP-34 | RAG `answer` agent — retrieve, generate, cite, refuse | ⬜ |
| EAP-35 | Vector isolation test — A cannot retrieve B's chunks | ⬜ |
| EAP-36 | Collaborator onboarding — CONTRIBUTING.md, add collaborator | ⬜ |

**EAP-35 is the one that matters**, for the same reason EAP-15 did. A similarity search
that forgets `tenant_id` returns another customer's documents as your answer, and it looks
like a working feature the entire time.

### Decisions locked at planning (revisit only in ADR-003)
1. **Embedding dimension 768.** It fixes the `vector(N)` column; changing it later means
   re-embedding the whole corpus behind a migration. 3072 costs 4× the storage for recall
   nobody at this corpus size can measure.
2. **No vector index until the row count justifies one.** On tens of chunks a sequential
   scan is faster than an index and exactly correct. IVFFlat built on an empty table trains
   its lists on nothing and returns poor recall forever after.
3. **Retrieval spans the tenant's whole corpus**, with an optional `document_ids` filter —
   one extra WHERE clause, and it demos both ways.

---

### EAP-28 — Embeddings boundary
Mirror `src/eap/llm/base.py`: an `Embedder` Protocol with
`embed(texts: list[str]) -> list[list[float]]`, a `GeminiEmbedder` that is the only file
importing the vendor SDK, an `lru_cache`d `get_embedder()`, and a deterministic fake for
tests. Batch by default — one call per chunk is the mistake.
**Done when:** tests embed with the fake and never touch the network, and a missing key
raises the same `LLMError` shape `get_llm()` does.

### EAP-29 — documents + document_chunks
`documents` (tenant_id, external_id, title, source, metadata JSONB) and `document_chunks`
(tenant_id, document_id, ordinal, content, `embedding vector(768)`, token_count).
Unique on `(tenant_id, external_id)` and `(document_id, ordinal)`. Copy the RLS block from
`0006` verbatim: ENABLE + FORCE + `tenant_isolation` on both tables.
**Done when:** `alembic upgrade head` and `alembic check` are both clean, and `eap_app` can
read and write both tables — `0005`'s DEFAULT PRIVILEGES should cover it, so verify rather
than assume.

### EAP-30 — Chunking
Pure function, no DB, no LLM, no I/O: `chunk(text, size, overlap) -> list[str]`.
Tests: empty, shorter than one chunk, exact multiple, overlap actually overlaps, no chunk
exceeds size. The only piece of M3 fully testable without Postgres or a model — which is
why it is the first ticket to hand a new contributor.

### EAP-31 — POST /v1/documents
Accept title + text + optional `external_id`; chunk, embed the batch, insert the document
and its chunks in one transaction.
**Done when:** posting the same `external_id` twice leaves one document and the same chunk
count, not duplicates.

### EAP-32 — Retrieval
`search(session, tenant_id, query, k, document_ids=None)` — embed the query, `ORDER BY
embedding <=> :q` with `vector_cosine_ops`, `LIMIT k`. No index yet, per decision 2.
**Done when:** a test seeds known chunks and asserts the nearest one comes back first.

### EAP-33 — ADR-003 — vector index and RAG design
Context / Decision / Consequences on: the dimension choice, cosine over L2, the index
deferred and the row count that should trigger HNSW, embedding stored on the chunks table
rather than a separate vector store, and what a re-embed would actually cost.

### EAP-34 — RAG answer agent
New agent `answer`: retrieve top-k for the question, build a prompt from the chunks,
generate, and return the chunk ids it used. If the best distance is worse than a threshold,
say the corpus does not cover the question instead of answering — the same discipline as
docwriter's Known Limitations rule.
**Done when:** a `ScriptedLLM` test proves the refusal path fires on weak retrieval.

### EAP-35 — Vector isolation test
Seed two tenants with deliberately near-identical text. Assert tenant A's search returns
only A's chunks, at the HTTP layer, running as `eap_app`. The RLS policy should make this
pass for free — the test exists to prove it did not silently get turned off.

### EAP-36 — Collaborator onboarding
CONTRIBUTING.md (branch naming, conventional commits, ticket refs, how to bring the stack
up), add the collaborator on GitHub, confirm branch protection still demands a green PR
from his account. First ticket for him: EAP-30 — no database and no API key needed.