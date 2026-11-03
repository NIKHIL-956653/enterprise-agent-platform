# Backlog

Every commit references a ticket id. Tickets are closed by a merged PR, not by being "done".

Status: `✅ done` · `🔨 in progress` · `⬜ todo` · `⏸️ deferred` · `➡️ superseded`

Last updated: 2026-11-02, pilot readiness (EAP-62→66) opened; EAP-61 merged.

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


EAP-37 closed 2026-11-01: `/v1/runs` and `/v1/documents` now hand out a `(created_at, id)`
cursor (`eap.api.pagination`), so two rows on one timestamp straddling a page boundary are
neither skipped nor repeated. The UI already passed `next_before` back unchanged, so nothing
in `web/` moved; a bare timestamp is still accepted with its old "strictly older" meaning.

EAP-38 closed 2026-11-01: migration `0005` now creates `eap_app` with the password inside
`APP_DATABASE_URL` - the URL the app logs in with, so the two cannot drift - via a
transaction-local setting and `format('%L')`, never string-built SQL. Unset means the old dev
default, so local setup is unchanged and nothing secret is committed. Rotation is
`ALTER ROLE eap_app PASSWORD` plus the URL, documented in `.env.example`.

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
| EAP-L3 | Locale formatting — digits per tenant, AED both languages, Asia/Dubai datetimes, bidi isolates | ✅ |
| EAP-L4 | Runner records `language` on every run; `GET /v1/runs?language=` | ✅ |
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

EAP-L3 landed as a rules table of our own rather than Babel. The data is CLDR's - every month,
weekday, AM/PM marker and separator was checked against it - but the patterns are how UAE
listings read ("AED 2,500,000", "2,500,000 درهم"), and no formatter emits hidden direction
marks: `isolate()` is the only source of those. The digit system is a per-call argument -
Western by default, which is CLDR's own default for ar-AE, Arabic-Indic as an opt-in - and the
per-tenant column arrives with its first consumer, EAP-L12. Money is `int` or `Decimal` only,
and fils are never rounded away. `tzdata` is now a declared dependency: Windows has no time
zone database of its own, and the venv only had it by accident (via optional extras).

EAP-L4 decides a run's language once, at submission, from every string in the input payload
joined - no agent names a "language field", so a new agent is covered the day it registers.
No confident signal ("ok", a bare number, an empty payload) means the tenant's
`default_language`, so the column always holds a real language and `?language=` has no
"unknown" bucket. The filter is typed as the enum, so `?language=fr` is a 422 at the door.
Migration `0015` adds `(tenant_id, language, created_at DESC)` so a filtered page is the
same single range scan as an unfiltered one.

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
| D3 | Audit event payloads render as empty boxes on the run detail page | ✅ |
| D4 | Human-readable `external_id` for UI uploads (slug, not `upload:<uuid>`) | ✅ |
| D5 | Typed run forms for every agent (echo rejects plain text today) | ✅ |
| D6 | Optional External ID field on the upload form | ✅ |
| D7 | Prompt: no "According to the passage" openers, plus a test | ✅ |
| D8 | Confirm echo's 0 ms latency is a measurement, not a missing value | ✅ |
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

### D5 — how it closed
The four hand-written forms and the raw-JSON fallback are gone. `GET /v1/agents` now carries
each agent's `input_schema` - its pydantic InputModel as JSON Schema - and one `AgentForm`
renders labelled fields from it, with presentation hints (labels, textareas, an "Advanced"
section) kept in a small map in the UI. Required-ness and limits come from the schema, so the
form cannot drift from what the API validates; an agent with no hints still gets a typed form.
Errors are checked in the browser first and shown under the offending field; a 422 the API
still returns is mapped onto the field its `loc` names. Echo takes plain text.
Not covered by CI: `web/` has no test runner, so the form behaviour was checked with a
Playwright script against the running stack (16 checks), not a committed test.

### D3 — how it closed
The payloads were never empty: the database, the API and even the DOM all carried them.
Each event was a `<details>` collapsed by default, and the stylesheet removed its disclosure
marker, so nothing on the row said the JSON was one click away. A closed `<details>` is not
rendered, which is also why select-all + copy picked up Input and Output but skipped every
payload. The run page now renders each payload as key/value lines that are always on screen:
agent and input for `run.started`; prompt and completion clamped to 200 characters with a
"show more" toggle for `llm.call`; query, top-k, floor and the retrieved chunks for
`retrieval.performed`; status and latency for `run.finished`. Keys outside those lists are
still shown, generically, so the view never silently drops part of a hashed payload, and an
empty payload renders no block at all. Nothing touches `audit_log`: no route, method or
migration. `tests/test_run_audit.py` now asserts every event's payload is populated over the
API, so a future empty box is known to be a rendering fault. `web/` is outside CI; `tsc -b`,
`vite build` and a Chromium check were run by hand.

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

---

### D8 — how it closed
Confirmed from the code, no change needed. `runner.py` measures every run the same way:
`started = time.perf_counter()` before the agent's `run()`, and `run.latency_ms =
int((time.perf_counter() - started) * 1000)` after it. Echo does no I/O and finishes in
well under a millisecond, so `int()` rounds a real sub-millisecond measurement down to 0.
The page shows "0 ms" for the same reason a stopwatch shows 0.0 s for a blink.

---

## Pilot readiness — EAP-62→66 🔨

About twenty students across the three colleges log in and ask questions against their own
college's documents. What the demo did not need and a pilot does: an account per student,
a way for students to say whether an answer helped, a cap on what a day can cost, a view of
how the pilot is going, and a way to run it off one VPS. Same rules as the demo work: built
on main as ordinary ticket branches, nothing touches the L-series.

| ID | Title | Status |
|---|---|---|
| EAP-62 | Per-student accounts from `demo-docs/{tenant}/students.csv`; credentials file, gitignored | ✅ |
| EAP-63 | Answer feedback - `feedback` table, `POST /v1/runs/{id}/feedback`, thumbs on the answer card | ✅ |
| EAP-64 | Daily token budget per tenant, counted in Asia/Kolkata, 429 with the reset time | ✅ |
| EAP-65 | Ops summary - `GET /v1/ops/summary` for a tenant's admin + `scripts/ops_report.py` for the operator | ✅ |
| EAP-66 | Deployment pack - `deploy/compose.prod.yml`, Caddy, `.env.production.example`, `docs/DEPLOY.md` | 🔨 |

### EAP-62 — how it closed
`scripts/seed_students.py`, a sibling of `seed_demo.py` rather than a flag on it: the demo
seed describes fixed tenants in code, the student roster is data that changes per college.
It reads `demo-docs/{tenant}/students.csv` (`name,email`, any column order), creates one
`member` at clearance 1 per row through the owner session, and writes
`demo-docs/{tenant}/credentials.txt` (`email<TAB>name<TAB>password`, mode 0600). Decisions:

- **The file is the only place a password exists.** stdout gets counts and the path;
  `tests/test_seed_students.py` reads the file back to log in and asserts no password it
  finds there appears in stdout. `demo-docs/` was already gitignored; `credentials.txt` is
  now ignored by name as well, wherever it sits.
- **Idempotent, with `--rotate` as the only way to change a password.** A re-run re-asserts
  `member`/clearance 1 (the same repair `seed_demo.py` does) and keeps the hash; the line
  on file is kept too. Dropping a row from the CSV deletes neither the account nor its line.
- **Staff emails are skipped, not demoted.** A CSV row naming an existing admin or owner is
  reported and left alone, so a roster cannot turn an admin into a student or reset their
  password.
- **`name` goes to the credentials file only.** `users` has no name column, and adding one
  for a hand-out slip is not this ticket; the admin gets the name next to the login.

Not verified in CI: a run against the real `demo-docs/` folders - the tests use a two-row
CSV in a temporary directory.

### EAP-63 — how it closed
Migration `0013`: `feedback(id, tenant_id, run_id, user_id, verdict, comment, created_at)`,
RLS + FORCE with the same `tenant_isolation` policy as every tenant table, `verdict` held to
`up`/`down` by a named CHECK (`ck_feedback_verdict`, by hand, as in `0011`), and
`(run_id, user_id)` unique so a person has one vote per run - `POST /v1/runs/{id}/feedback`
upserts on it, so a changed mind rewrites the row and resets `created_at`. Two decisions:

- **Whose run it is, nobody checks in Python.** The handler looks the run up with no tenant
  filter; RLS hides another tenant's run, so the answer is the same 404 as `GET /v1/runs/{id}`,
  never a 403 that would confirm it exists. `tests/test_feedback.py` asserts status AND body
  equal those for a run that never existed.
- **The database refuses a cross-tenant vote on its own.** The foreign key to `agent_runs` is
  composite, `(tenant_id, run_id)` against a new `(tenant_id, id)` key on `agent_runs`: the
  RLS `WITH CHECK` pins the row's `tenant_id` to the caller, so a run from another tenant has
  no parent and the INSERT fails even with the lookup deleted. Tested by inserting as
  `eap_app` in tenant B's context against A's run, bypassing the endpoint.

The answer card in the admin UI has thumbs; a thumbs down opens an optional one-line comment.
`web/` is outside CI; `tsc -b` and `vite build` were run by hand, the click path was not.

### EAP-64 — how it closed
Checked first, as asked: EAP-46 counts **requests** per minute in Redis; nothing counted
tokens. Worse, `agent_runs.prompt_tokens` and `completion_tokens` had never been written -
the `LLM` Protocol returned text alone, so every run on record says 0. So this ticket is
two things:

- **Token accounting.** `Completion(text, prompt_tokens, completion_tokens)` and a
  `MeteredLLM` Protocol (`complete_metered`) next to `LLM`. Gemini and Anthropic implement it
  from their own usage blocks (thinking tokens count as output; cache tokens as input). The
  runner's auditing wrapper prefers it and falls back to `complete` plus a 4-chars-per-token
  estimate - never zero for a non-empty call, because a budget that counts nothing for an
  unmetered model is no budget. Counts sum onto the run row, flushed after every call, and
  land in the `llm.call` audit payload with `usage: reported|estimated`. Older test fakes
  keep working unchanged.
- **The cap.** `tenants.daily_token_budget` (migration `0014`; default 500,000, CHECK ≥ 0;
  0 means no cap, an explicit operator choice, never the default). Spend is
  `SUM(prompt_tokens + completion_tokens)` over the tenant's runs since 00:00 in
  `settings.token_budget_timezone` (Asia/Kolkata), read under RLS - the runs table is the
  ledger, no Redis counter to drift from it. Checked in two places: the run endpoint
  refuses with **429** and a plain message ("…resets at 00:00 IST (in about 6 h 30 min)…")
  plus `Retry-After`, and the runner asks again before **each** model call, so a run already
  executing fails with the same words and no model call. The endpoint refuses every run once
  the budget is gone, echo included: "this college is out of tokens today" is one truth.
  It fails closed - no visible tenant row reads as exhausted - the opposite of EAP-46,
  deliberately: that limiter protects availability, this one protects a bill.

`seed_demo.py` gives the three colleges 5,000,000 a day (a ceiling nobody should meet while
still being a ceiling); `acme` keeps the default. `tests/test_token_budget.py` (16) covers the
accounting, both providers' usage parsing, the Kolkata day boundary (a run at 23:59 yesterday
does not count, one at 00:01 does), the 429 and its message, zero, tenant separation, and the
in-worker refusal through `execute_agent`.

### EAP-65 — how it closed
One function, `eap.ops.summarize`, two callers, and the split between them is the design:

- **`GET /v1/ops/summary?days=7`** for a tenant's own admin or owner (403 for a member, by
  the role claim). Runs per day (every day of the window, zeros included), p50/p95
  `latency_ms`, refusal rate (`answered: false` over succeeded `answer` runs), error rate
  (failed + timed out over runs), tokens, thumbs up/down and their ratio. It passes the
  RLS-scoped session and **no tenant id**: the function's SQL has no tenant clause on this
  path, so the numbers are the caller's by construction. An **Ops** tab in the admin UI
  (admins only in the nav) renders the table and one bar per day; `web/` is outside CI,
  `tsc -b` and `vite build` by hand.
- **`scripts/ops_report.py`** for the platform operator: the owner session, one call per
  tenant with an explicit `tenant_id` (the owner bypasses RLS, so that filter is the whole
  scoping there), all tenants as columns. The docstring says why: the app has no
  cross-tenant endpoint and must never grow one - no token, however privileged, can ask the
  API for another tenant's numbers; the one party entitled to see all tenants already holds
  the owner credentials, so that view goes through the database, never through the app.
  `test_the_app_has_no_cross_tenant_route` pins it: `/v1/ops/summary` is the only ops route,
  and `?tenant=`/`?tenant_id=`/`?all=` change nothing.

Days are calendar days in `settings.token_budget_timezone`, the same calendar the budget
uses, so "today" means one thing on the Ops tab and in a 429. Rates are `null`, not 0, when
the denominator is 0. `tests/test_ops_summary.py` (14) builds a known history (7 runs, 1
error, 2 refusals, 125 tokens, 1/1 thumbs) and checks every number through both callers,
plus the window arithmetic and a run at 23:55 IST landing on yesterday's bar.

### EAP-66 — the deployment pack (PR open, left for review before it is followed)
`deploy/compose.prod.yml` runs every service the local compose runs - Postgres+pgvector,
Redis, Temporal auto-setup, Temporal UI (unpublished) - plus the API, the worker and Caddy.
Only Caddy publishes ports; it terminates HTTPS for `$DOMAIN`, serves the built `web/` as
static files and proxies `/v1`, the probes and `/docs` to the API on the same origin
(`deploy/Caddyfile`, validated with `caddy validate`). `deploy/caddy.Dockerfile` builds the
UI with `VITE_API_BASE` empty, so no hostname is baked into the bundle. The API image now
carries `alembic/`, `alembic.ini` and `scripts/` (plus the `scripts` extra), so migrations,
seeds, ingest and the ops report run as one-off containers from the image the API runs; a
`.dockerignore` keeps `.env*`, `demo-docs/` and `node_modules` out of every layer.
`.env.production.example` names every setting and holds no values; `.env.production` is
gitignored. `docs/DEPLOY.md` is the runbook: provision, DNS, clone, env, up, migrate, seed,
ingest, verify `/readyz`, each with a "done when"; then a one-line nightly `pg_dump` cron,
the restore, day-to-day operations, and what is logged about students' questions and what
the colleges must be told. Nothing in the pack is a real hostname, key or password; the
only hostnames are `example.edu` placeholders. Not verified: an actual `docker compose up`
(no Docker daemon in the environment the pack was written in) - the compose file was parsed
and the Caddyfile validated, nothing more. Follow the runbook once on a scratch VPS before
the pilot's.

---

## Known limits — recorded, not scheduled

Four things that are true of the platform today and should be read as known rather than
missed. None is demo-blocking; two are security-relevant. Recorded 2026-11-01.

| ID | Title | Why it waits |
|---|---|---|
| EAP-58 | Vector retrieval has no index — and adding HNSW needs a decision first | No tenant is near ADR-003's trigger (p95 > 200 ms or 100k chunks) |
| EAP-59 | Per-tenant provider keys are plaintext at rest in the only secret backend | Dev/demo backend by design; a KMS or Vault backend is a milestone, not a ticket |
| EAP-60 | Branch protection on `main` does not require the CI check to pass | Repo setting, no code; red PRs #25–#35 merged because of it |
| EAP-61 | `POST /v1/documents` is an existence oracle for restricted `external_id`s | ✅ closed by ADR-006 + migration `0012` |

### EAP-58 — Vector retrieval has no index
**Type:** scaling · **Status:** ⬜ todo

`retrieve()` runs `ORDER BY c.embedding <=> CAST(:q AS vector)` with no vector index
(`src/eap/rag/retrieval.py`; migration `0007` creates only the b-tree
`(tenant_id, document_id)` index). Two precisions on the usual framing of this: the scan is
not over the whole table but over the caller's tenant - RLS filters on `tenant_id` and the
b-tree index serves that filter - and it is exact, which is why the isolation and clearance
tests can assert *empty* results rather than *poor* ones. The cost is one distance
computation per chunk the tenant owns, per question. That grows linearly with a tenant's
corpus, so the first tenant to upload a real library (tens of thousands of chunks) is the
problem, more than the number of tenants. ADR-003 §4 chose this deliberately and set the
trigger for HNSW: p95 retrieval above 200 ms, or 100k chunks.

**The catch, which is why this is a ticket and not a one-liner.** An HNSW index on
`document_chunks.embedding` is global. It returns the `ef_search` nearest vectors across
*every* tenant, and only then does RLS strip the rows the caller may not see. A tenant whose
own corpus holds a good match can get zero rows back because the index handed up another
tenant's neighbours and RLS removed them all. The `answer` agent would then refuse - "the
documents do not cover that question" - while the document sits right there. That is a
correctness failure that looks like a feature, the exact shape EAP-35 was written to catch,
and the existing tests would not catch it: they assert the *absence* of other tenants' rows,
which RLS still guarantees, not the *presence* of your own.

Two ways through, and the choice has to be made **before** the index is built, because each
implies a different table layout and a different cut-over:

1. **pgvector iterative index scans** (0.8.0+; the CI/compose image ships `0.8.7`):
   `SET hnsw.iterative_scan = relaxed_order` makes the index keep scanning until enough rows
   survive the filter, bounded by `hnsw.max_scan_tuples`. One global index, no schema change,
   but recall under a highly selective filter (a small tenant in a big table) is bounded by
   that tuple cap, and the setting is per-session - it belongs in `get_tenant_session`, next
   to the RLS context, or it is forgotten.
2. **Partition `document_chunks` by `tenant_id`** (list partitioning, one HNSW index per
   partition). "Nearest within the tenant" becomes what the index computes, not something
   filtered afterwards; RLS still applies on top. Costs: partition management on tenant
   creation (which EAP-16 does not have yet), a schema migration with a data copy, and
   per-partition index builds.

Whichever is chosen, two things come with it: the ops class must match the operator
(`vector_cosine_ops` for `<=>`), and `tests/test_retrieval.py` needs a *presence* test - a
small tenant inside a large multi-tenant table still finds its own nearest chunk - so that
approximate retrieval cannot pass the suite while silently refusing real questions.

### EAP-59 — Per-tenant provider keys are plaintext at rest
**Type:** security · **Status:** ⬜ todo

EAP-47 put the key lookup behind a `SecretResolver` Protocol, which is the right shape, but
the only implementation is `EnvSecretResolver` (`src/eap/llm/secrets.py`): it reads
`TENANT_<SLUG>_<NAME>` from the process environment, then from `.env`. So a tenant's
Anthropic or Gemini key lives as plaintext in a file on the host, readable by anything that
can read the file, and `secret_backend` accepts no other value (`get_secret_resolver` raises
on anything but `env`). Encryption at rest is therefore not "unconfirmed": for the backend
that exists, there is none. That is fine for a demo on one laptop and not fine for a
deployment holding customer keys. The resolver boundary means a KMS- or Vault-backed
implementation is additive - no call site changes - but it is a milestone with its own
threat model (who can rotate, who can read, how a leaked key is revoked), not a ticket.

### EAP-60 — Branch protection does not require the CI check
**Type:** process · **Status:** ⬜ todo

PRs #25–#35 all merged with the `test` check red (the Temporal service never started in
CI; fixed in #37). Branch protection on `main` therefore does not have "Require status
checks to pass before merging" enabled with `test` selected - or has it, with the check not
marked required. Fix in the repository settings, no code: require `test`, and consider
"Require branches to be up to date before merging" so a green PR cannot land on a main that
moved. Until then, "CI must be green before you merge" is a habit, not a property.

### EAP-61 — The document write path reveals restricted `external_id`s
**Type:** security · **Status:** ✅ done (ADR-006, migration `0012`)

Reproduced 2026-11-01 on the demo seed. `admin@engg.edu` ingests "Exam answer key" with
`external_id` `exam-key` at classification 3. `student@engg.edu` (clearance 1) lists
documents and sees `[]` - correct. The student then POSTs their own document with
`external_id` `exam-key` at classification 1 and gets `403 classification exceeds your
clearance`; the same POST with `not-taken` gets `201`. The 403 is RLS refusing the
`ON CONFLICT DO UPDATE` on a row the student cannot see, and the handler's only message for
a refused write names clearance. So an authenticated member can test whether a restricted
document with a given id exists - existence only, never content - and with
`scripts/ingest_pdfs.py` ids are file names, which are guessable (`exam-key.pdf`). That is a
small crack in exactly the claim the demo makes ("never told it exists").

Why it is not a patch: `(tenant_id, external_id)` is unique across classifications, so a
collision with an invisible row has no honest non-leaking response - 201 would be a lie, 409
is the same oracle with a different number. Options, each a semantics decision:
(a) scope uniqueness to `(tenant_id, classification, external_id)` - closes the oracle,
but re-posting at a new classification then creates a second document instead of
reclassifying the first; (b) keep the contract and make probing *detectable*: audit a
`document.write_refused` event with the attempted id, and rate-limit refused writes per
user; (c) namespace auto and explicit ids per clearance level. D4 was designed so the
auto-generated ids never touch this (random suffix, `DO NOTHING`); explicit ids still do.

**Closed 2026-11-01.** ADR-006 records the options. Chosen: uniqueness becomes
`(tenant_id, classification, external_id)`, so a re-post only ever conflicts with a row at
the level being written - one the caller can see - and before the upsert every copy of that
id the caller *can* see at another level is deleted, so "re-posting replaces" still holds
across a reclassification. A student probing a restricted id and a free id now get responses
identical in status, headers and body apart from generated ids; their own re-post still
replaces; an admin's re-post at a new level still moves the document. Consequence, on the
record: one `external_id` can exist at several classifications within a tenant. The
downgrade is reversible only while that has not happened, and fails loudly otherwise.
