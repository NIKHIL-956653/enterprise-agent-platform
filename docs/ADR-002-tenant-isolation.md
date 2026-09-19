# ADR-002 — Tenant isolation model

**Status:** Accepted · **Date:** Sprint 1 · **Decider:** Nikhil Vijayapuri

---

## Context

eap serves multiple organisations from one deployment. Tenant isolation is a **security
property**, not a feature: one tenant reading another tenant's data is a breach, not a bug.

Three patterns were considered. The industry calls the first **pooled** (shared
infrastructure, tenant discriminator) and the others **silo** (dedicated infrastructure).

| Option | Isolation guaranteed by | Cost |
|---|---|---|
| Shared tables + `tenant_id` + Row-Level Security | **The database** | Low |
| Shared tables + `tenant_id`, filtered in application code | Developer discipline | Low |
| Schema or database per tenant | Physical separation | High |

## Decision

**Pooled: shared tables, a `tenant_id` column on every tenant-owned table, enforced by
PostgreSQL Row-Level Security.**

Each request sets a session-local variable (`app.tenant_id`) from the authenticated JWT.
RLS policies on every tenant table restrict all reads and writes to matching rows.

The critical property: **a query that forgets to filter by tenant returns zero rows, not
another tenant's data.** The guarantee lives below the application, so it holds even when
the application is wrong.

Rules that follow from this:

1. `tenant_id` is **never** read from a request body, query string or header — only from
   the verified token. A caller must not be able to name the tenant they want.
2. Every tenant-owned table gets `tenant_id NOT NULL`, an RLS policy, and an index with
   `tenant_id` as the leading column.
3. The session variable is set per request, and **reset when the connection returns to the
   pool** — a pooled connection carrying a stale tenant id is the one way this design
   leaks.
4. The application migration role owns the tables and bypasses RLS; the **application
   runtime role does not**. Postgres table owners are exempt from RLS by default, so
   running the app as the owner would silently disable every policy.

## Alternatives rejected

**Application-level filtering only.** Every query must remember `WHERE tenant_id = ...`.
One missed filter in one endpoint — or one raw SQL string, or one ORM relationship loaded
without a filter — is a cross-tenant leak, with nothing underneath to catch it. The
guarantee is only as strong as the least careful line of code ever written. Rejected: a
security property must not depend on discipline.

**Schema or database per tenant.** Strong isolation and easy per-tenant export or delete,
but every migration must run across every schema, connection routing becomes stateful, and
it degrades badly past a few hundred tenants. Rejected as a default — kept as the upgrade
path below.

## Consequences

**Good**

- Isolation is a database guarantee, testable directly: connect as a tenant, try to read
  another's row, get nothing.
- One schema, one migration run, one backup, one connection pool.
- Scales to thousands of tenants on a single database.
- Per-tenant usage and cost are a `GROUP BY tenant_id` away.

**Bad — accepted**

- **RLS + connection pooling is the sharp edge.** The session variable must be set at the
  start of every request and cleared at the end. Forget the reset and a recycled connection
  serves the previous tenant's context. This is handled in exactly one place — the session
  dependency — and covered by a test.
- Two database roles to manage (migration owner, restricted runtime).
- RLS adds a small per-query cost.
- Noisy-neighbour risk: one heavy tenant can affect others. Addressed by rate limits in M4.

## Upgrade path

If a customer ever requires physical separation, the **same schema and the same code**
deploy into a dedicated database for that tenant. Pooled and silo coexist; routing is a
connection-string lookup. No rewrite.

## Revisit when

- A single tenant's data volume degrades shared-table query performance.
- A compliance requirement demands physical separation (then: silo that tenant only).
- RLS policy management becomes a bottleneck across many tables.