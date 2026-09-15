# Backlog

Every commit references a ticket id. Tickets are closed by a merged PR, not by being "done".

Status: `✅ done` · `🔨 in progress` · `⬜ todo`

---

## Sprint 0 — M0 Skeleton

| ID | Title | Status |
|---|---|---|
| EAP-1 | Project scaffold — src layout, `pyproject.toml`, ruff + pytest config, `.gitignore` | ✅ |
| EAP-2 | Core services — settings, async engine, Redis pool, structlog, request-id middleware | ✅ |
| EAP-3 | Health API — `/livez`, `/readyz`, app factory with lifespan | ✅ |
| EAP-4 | Local stack — docker-compose (pgvector + Redis, healthchecks), Dockerfile, dev script | ✅ |
| EAP-5 | Alembic — async env, naming convention, `0001` baseline enabling pgvector | ✅ |
| EAP-6 | Test suite + CI — 4 integration tests, GitHub Actions running lint/migrate/test | ✅ |
| EAP-7 | Repo hygiene + README — `.gitattributes`, LF normalisation, project README, this backlog | 🔨 |

---

## Open

### EAP-8 — Write PRD and ADR-001 (stack)
**Type:** docs · **Priority:** high

Capture *why* before it's forgotten. `docs/PRD.md` (problem, users, scope, non-goals) and
`docs/ADR-001-stack.md` (why Postgres+pgvector over a dedicated vector DB, why async
throughout, why Alembic over `create_all`).

**Done when:** both files exist, ADR follows Context / Decision / Consequences, README
links to them.

---

### EAP-9 — `/readyz` returns 200 while degraded
**Type:** bug · **Priority:** high · *Found during M0*

`/readyz` reports `{"status": "degraded"}` in the body but still answers **HTTP 200**.
Load balancers and Kubernetes key readiness off the status code, not the body — so with
Postgres down, the pod keeps receiving traffic it cannot serve.

**Fix:** return `503` when any dependency check fails. Body stays the same so the response
is still diagnosable.

**Done when:** a test asserts 503 with a dependency stubbed to fail, and 200 when healthy.

---

### EAP-10 — Health checks can starve the connection pool
**Type:** bug · **Priority:** medium · *Found during M0*

`/readyz` checks out a real pooled connection per call. Under load every connection is
busy serving requests, so the probe blocks, times out, and the orchestrator pulls a pod
that was merely busy. Its traffic shifts to the remaining pods, which saturate faster and
fail too — a cascading outage caused by the monitoring, not the fault.

**Fix:** cache the dependency check result for ~5s and serve it in between. Consider a
dedicated 1–2 connection pool for probes.

**Done when:** N concurrent `/readyz` calls produce at most one DB round trip per window,
proven by a test.

---

## Sprint 1 — M1 Tenants + Auth (planned)

| ID | Title |
|---|---|
| EAP-11 | `tenants` table + migration `0002`; slug, name, status, timestamps |
| EAP-12 | `users` table scoped to a tenant; password hashing (argon2) |
| EAP-13 | JWT issue + verify; `/v1/auth/login`, `/v1/auth/me` |
| EAP-14 | `get_current_tenant` dependency — `tenant_id` resolved from the token, never the request body |
| EAP-15 | Tenant isolation enforced at the query layer, with a test proving tenant A cannot read tenant B |
| EAP-16 | Tenant CRUD API for admins |

**EAP-15 is the one that matters.** Multi-tenancy is a security property, not a column.
It needs a test that actively tries to break it.