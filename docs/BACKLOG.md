# Backlog

Every commit references a ticket id. Tickets are closed by a merged PR, not by being "done".

Status: `✅ done` · `🔨 in progress` · `⬜ todo` · `⏸️ deferred` · `➡️ superseded`

Last updated: 2026-10-27, M6 started.

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
| EAP-27 | docs: BACKLOG catch-up (M0–M2) + M3 sprint plan | ✅ |
| EAP-28 | Embeddings boundary — `Embedder` Protocol, `GeminiEmbedder`, fake | ✅ |
| EAP-29 | `documents` + `document_chunks`, migration `0007`, RLS + FORCE | ✅ |
| EAP-30 | Chunking — pure function, overlap, boundary tests | ✅ |
| EAP-31 | `POST /v1/documents` — chunk, batch-embed, store | ✅ |
| EAP-32 | Retrieval — cosine top-k scoped to the tenant | ✅ |
| EAP-33 | ADR-003 — vector index choice and RAG design | ✅ |
| EAP-34 | RAG `answer` agent — retrieve, generate, cite, refuse | ✅ |
| EAP-35 | Vector isolation test — A cannot retrieve B's chunks | ✅ |
| EAP-36 | Collaborator onboarding — CONTRIBUTING.md, add collaborator | ⏸️ |
| EAP-46 | Per-tenant rate limiting on the run endpoint (pulled forward from M4) | ✅ |

**EAP-35 was the one that mattered**, for the same reason EAP-15 did. A similarity search
that forgets `tenant_id` returns another customer's documents as your answer, and it looks
like a working feature the entire time. The test asserts an empty result, not a poor one.

Closed 2026-10-13: 7 migrations, 86 tests green on main.

Two things landed differently from the plan. Retrieval gained a `min_similarity` floor that
was not in the EAP-32 description — without it top-k always returns k rows however bad they
are, and EAP-34's refusal path has nothing to stand on. And `AgentContext` gained a narrow
`retriever` closure rather than a session, keeping the EAP-17 rule intact: an agent can
search its own tenant's chunks and do nothing else.

---

### EAP-36 — Collaborator onboarding ⏸️
**Type:** process · **Deferred:** end of M3

Written for a second contributor who did not materialise. The repo stays solo, so branch
protection and a green PR are the only ceremony that earns its keep.

**Revisit when:** someone actually commits. CONTRIBUTING.md is the first thing they would need.

---

## Sprint 4 — M4 Enterprise hardening ✅ (EAP-48/49 deferred)

Three things gate any serious enterprise deployment, and this sprint built all three:
per-user authorization inside a tenant (ABAC), a real audit trail, and durable execution.
They outranked the admin UI, which reordered the original M4/M5 plan — compliance moved
forward, UI moved back to Sprint 5.

| ID | Title | Status |
|---|---|---|
| EAP-40 | ABAC — document classification + principal in the RLS predicate | ✅ |
| EAP-41 | Audit trail — append-only, hash-chained, INSERT-only role | ✅ |
| EAP-42 | Audit capture points + PII redaction policy | ✅ |
| EAP-43 | Temporal — runner as workflow, activities, worker deployment | ✅ |
| EAP-44 | Step-level checkpointing + idempotency keys on run requests | ✅ |
| EAP-45 | Provider backoff, circuit breaker, bounded per-tenant concurrency | ✅ |
| EAP-47 | Per-tenant provider keys via secrets manager (unblocks EAP-26) | ✅ |
| EAP-50 | Anthropic as a second provider behind the LLM Protocol (added mid-sprint) | ✅ |
| EAP-48 | Hybrid retrieval — BM25 alongside vectors, merged by RRF | ⏸️ |
| EAP-49 | Eval harness — context precision and faithfulness on a fixed set (→ M8, EAP-L16→L21) | ➡️ |

### How it closed (2026-10-22)
Everything above except EAP-48/49 is merged (PRs #22–#29). EAP-42 was written
transport-agnostic as planned, so EAP-43 did not force a rebuild. EAP-50 arrived mid-sprint
when real Anthropic credits became available; the first live model run then exposed hidden
SDK retries, fixed in #29 (one retry policy, ours). Live opt-in smoke tests now exist:
`pytest --live -m live` — four real runs, a few cents. EAP-48/49 stay deferred until the
conditions below are met.

---

## Sprint 5 — M5 Admin UI ✅

Last milestone of the English-only scope. Three small backend tickets unblock a Vite + React
+ TS app in `web/`; then four screens, a live-demo screenshot, and the LinkedIn post.

| ID | Title | Status |
|---|---|---|
| EAP-51 | CORS middleware — origins from settings, prod refuses the localhost default | ✅ |
| EAP-52 | `GET /v1/documents` — keyset-paginated list (no bodies), RLS/clearance scoped | ✅ |
| EAP-53 | `GET /v1/runs/{id}/audit` — the run's hash chain + computed `chain_ok` | ✅ |
| EAP-54 | `web/` scaffold — Vite + React + TS, login screen, token handling | ✅ |
| EAP-55 | Agents screen — list, typed run forms, async submit + poll | ✅ |
| EAP-56 | Runs screen — history with filters + keyset paging; run detail with audit chain | ✅ |
| EAP-57 | Documents screen — list + upload with classification | ✅ |

### (Sprint 4) Sequencing note
EAP-43 reshapes how a run executes, so audit capture points built before it would be
rebuilt after it. Either Temporal goes first, or EAP-42 is written transport-agnostic from
the start. Prefer the second: audit lands sooner and survives the migration.

### Open before these are committed
EAP-48 and EAP-49 are conditional. Hybrid retrieval matters only if users search exact part
numbers or policy IDs — vectors fail on rare literal strings, and nothing else fixes that.
Evals move from roadmap to blocking if a faithfulness score has to gate a deployment, which
also means someone has to own the threshold number.

Also scope-bearing and unanswered in general: where a deployment's documents live today
(connector work is the most underestimated piece of any RAG build), how often they change,
how many there are, and what latency users expect.

### How M5 closed (2026-10-22)
All seven merged (PRs #30–#33; EAP-54→57 landed together in #33). This backlog was not
updated in those PRs, which is why EAP-51→53 still read 🔨/⬜ after they shipped -
corrected 2026-10-27. v1.0.0 is not tagged yet.

### Numbering
Resolved differently from the note that stood here: the bilingual work (M6–M10) gets its
own series, EAP-L1→L29, instead of EAP-60+, so v2 reads as one sequence (see Sprint 6).

---

## Sprint 6 — M6 i18n foundation 🔨

First milestone of the bilingual English + Arabic build that ends at v2.0.0. M6 ships no
user-facing feature. It makes language a stored, first-class field on tenants, users, runs
and chunks, so M7's concierge has something to stand on. The rule for all of v2: i18n is
data, not branches - no `if lang == "ar"` in business logic.

Ids: v2 has its own series - M6 EAP-L1→L6 · M7 L7→L15 · M8 L16→L21 · M9 L22→L25 · M10 L26→L29.

| ID | Title | Status |
|---|---|---|
| EAP-L1 | Language detection — `Language` enum, deterministic Unicode-script `detect()`, confidence flag | ✅ |
| EAP-L2 | Migration `0011` — language on tenants, users, agent_runs, document_chunks + CHECK constraints | ⬜ |
| EAP-L3 | Locale formatting — digits per tenant, AED both languages, Asia/Dubai datetimes, bidi isolates | ⬜ |
| EAP-L4 | Runner records `language` on every run; `GET /v1/runs?language=` | ⬜ |
| EAP-L5 | Arabic-aware chunking — ؟ and ؛ end sentences, ، does not; per-chunk language tag | ⬜ |
| EAP-L6 | ADR-005 "i18n as data, not branches" + catalogue `{en,ar}.yaml` with a key-parity test | ⬜ |

EAP-L1 landed with three tunable thresholds, each pinned by a test: Arabic wins at a 20%
share of letters (so a Latin building name inside an Arabic question stays Arabic), needs at
least 2 Arabic letters (so one stray character can't flip a message), and a result is
`confident` only from 4 letters up - the bar EAP-L14's hysteresis will consume.
