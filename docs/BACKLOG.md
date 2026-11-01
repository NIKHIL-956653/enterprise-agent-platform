# Backlog

Every commit references a ticket id. Tickets are closed by a merged PR, not by being "done".

Status: `✅ done` · `🔨 in progress` · `⬜ todo` · `⏸️ deferred` · `➡️ superseded`

Last updated: 2026-10-31, demo readiness sprint (D1, D2 closed; CI repaired).

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
corrected 2026-10-27. v1.0.0 was tagged on the PR #33 merge (`89f8246`).

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
| EAP-L2 | Migration `0011` — language on tenants, users, agent_runs, document_chunks + CHECK constraints | ✅ |
| EAP-L3 | Locale formatting — digits per tenant, AED both languages, Asia/Dubai datetimes, bidi isolates | ⬜ |
| EAP-L4 | Runner records `language` on every run; `GET /v1/runs?language=` | ⬜ |
| EAP-L5 | Arabic-aware chunking — ؟ and ؛ end sentences, ، does not; per-chunk language tag | ⬜ |
| EAP-L6 | ADR-005 "i18n as data, not branches" + catalogue `{en,ar}.yaml` with a key-parity test | ⬜ |

EAP-L1 landed with three tunable thresholds, each pinned by a test: Arabic wins at a 20%
share of letters (so a Latin building name inside an Arabic question stays Arabic), needs at
least 2 Arabic letters (so one stray character can't flip a message), and a result is
`confident` only from 4 letters up - the bar EAP-L14's hysteresis will consume.

EAP-L2 landed with five named CHECKs written by hand in `0011`, and building it surfaced a v1
bug: SQLAlchemy 2.x `Enum(native_enum=False)` creates no CHECK unless `create_constraint=True`,
so `tenants.status`, `users.role` and `agent_runs.status` are validated in Python only - raw
SQL can write any string. The fix is a separate v1.0.x ticket, not part of M6.
`tests/test_language_columns.py` asserts the five new CHECKs exist by name, so the language
columns cannot quietly fall into the same state.

---

## Demo readiness — D1→D8 🔨

v1.0.0 was tested end to end on 2026-10-31: the engine works, the demo tooling did not
exist. The demo is three colleges, each its own tenant with its own subject PDFs, showing
that one college cannot see another's material and that a student cannot see faculty-only
material - and is never told it exists. Built on main as ordinary ticket branches; none of
it touches the L-series i18n work. D1→D3 block the demo; D4→D8 wait for them.

| ID | Title | Status |
|---|---|---|
| D1 | Three-college demo seed (`engg`, `mba`, `bba`; admin clearance 3, student clearance 1) | ✅ |
| D2 | PDF ingest script — folder per tenant, filename as `external_id`, skips scanned PDFs | ✅ |
| D3 | Audit event payloads render as empty boxes on the run detail page | ⬜ |
| D4 | Human-readable `external_id` for UI uploads (slug, not `upload:<uuid>`) | ✅ |
| D5 | Typed run forms for every agent (echo rejects plain text today) | ⬜ |
| D6 | Optional External ID field on the upload form | ✅ |
| D7 | Prompt: no "According to the passage" openers, plus a test | ✅ |
| D8 | Confirm echo's 0 ms latency is a measurement, not a missing value | ⬜ |
| EAP-39 | `seed_demo.py` printed a hardcoded agent list — closed by D1 | ✅ |
| CI | Temporal service never started in CI (`DB_PORT` unset) — red since EAP-43; PR #37, landed with D1 | ✅ |

### D1 — how it closed
`scripts/seed_demo.py` now upserts four tenants: `acme` unchanged, plus the three colleges.
Passwords come from `DEMO_PASSWORD` (environment, then `.env`) with the old dev default.
Enabled agents come from the registry, which closes EAP-39.

It also fixes the bug that made D1 urgent. The old upsert refreshed only `password_hash`,
so an `admin@acme.com` row created before migration `0008` kept the column's server
default, clearance 0, through every re-seed - an owner who could not upload a
classification-1 document. Role and clearance are now upserted too; re-running the seed
repairs such a row.

`tests/test_demo_seed.py` proves both demo claims on tenants created by the seed itself,
through `eap_app` and real JWTs. The strongest test puts classified documents in one
college, leaves another empty, and asserts a student in each sees byte-identical listings,
answers, run details and audit payloads: a 403, an "access denied", or any difference at
all would tell the student the document exists. Tests seed suffixed copies (`engg-1a2b…`)
so they never write fixtures into the real demo tenants on a shared dev database.


### CI was red from EAP-43 to D1
Found while landing D1: every CI run on main from PR #25 (EAP-43) to #35 failed, v1.0.0
included. `temporalio/auto-setup` waits on `nc -z $POSTGRES_SEEDS $DB_PORT` with no default
port; docker-compose set `DB_PORT`, the CI service block did not, so Temporal never started
and the ~57 tests that need a worker errored at setup. Locally everything passed, which is
how it went unnoticed. PR #37 adds the port and waits for the `default` namespace before
pytest; the first green run since 2026-10-14 was 241 passed. Branch protection did not
stop red merges - worth checking that "require status checks" is actually enabled.

### D2 — how it closed
`scripts/ingest_pdfs.py` posts through the API, never the database: the JWT picks tenant and
clearance and RLS checks the write. Decisions worth knowing:

- **Scans are skipped, not posted.** Below 100 non-whitespace characters, or 20 per page,
  a file counts as having no text (both thresholds are flags).
- **Long files are split, not truncated.** Above the API's 200,000-character body limit the
  text is posted in parts on paragraph boundaries. Part 1 keeps the bare file name as its
  `external_id`, so a file that grows keeps its document; a file that shrinks leaves its old
  trailing parts behind - delete them by hand, or wait for a delete endpoint.
- **No built-in password.** `--password`, then `INGEST_PASSWORD`, then `DEMO_PASSWORD`.
- `pypdf` is the `scripts` extra, not a runtime dependency - the API never parses PDFs.

Not verified in CI: extraction quality on real course PDFs (tests build their own) and real
embeddings (tests fake them). Both need one local run against `demo-docs/`.

### D7 — how it closed
Two layers, because a prompt alone only lowers the odds. The `answer` prompt now says to
start with the answer and never mention the passages, naming the phrase to avoid. Then
`strip_preamble()` removes a leading "According to the passage," / "Based on the provided
passages," / "The passage states that" and re-capitalises. It is deliberately narrow: only a
leading opener, never one carrying a `[n]` citation (stripping must not drop a source), and
anything unrecognised is left exactly as the model wrote it. The live suite now checks the
RAW completion from the run's audit trail, so `pytest --live` still tells you whether the
prompt itself works on a real model, not just the strip.

### D4 + D6 — how they closed
A document posted without an `external_id` now gets `slug(title)-<8 hex>`, e.g.
`marina-handbook-3f9a2c1d`, instead of `upload:<uuid>`; letters in any script survive the
slug, so an Arabic title keeps its letters. Two decisions worth knowing:

- **The suffix is random, and the insert is `ON CONFLICT DO NOTHING` with a retry.** So
  "omitted means a new document every time" still holds, and an auto id can never replace an
  unrelated document that shares a title. The alternative - a plain slug with a lookup for
  collisions - was rejected on purpose: the lookup would run under RLS, which hides documents
  above the caller's clearance, so an id scheme that asked "is `exam-key` taken?" would tell
  a student that a restricted document with that title exists.
- **Explicit ids are untouched.** Re-posting the same `external_id` still replaces the
  document's chunks (`test_reposting_the_same_external_id_replaces_it`), and
  `scripts/ingest_pdfs.py` keeps using the raw file name as its explicit id - the natural key
  of a file, where the title is the natural key of a UI upload.

The upload form gained an optional External ID field with a hint, and the success banner
names the id the API chose. When the id returned is already on screen the banner says
"Replaced", so the live demo is: upload with an id, upload again with the same id, one row,
flat chunk count. `web/` is outside CI; `tsc -b`, `vite build` and a Chromium check were run
by hand.
