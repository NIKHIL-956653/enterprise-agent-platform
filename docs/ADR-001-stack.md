# ADR-001 — Platform stack

**Status:** Accepted · **Date:** Sprint 0 · **Decider:** Nikhil Vijayapuri

---

## Context

eap runs AI agents for multiple tenants. That shapes every technology choice:

- **Agent work is I/O-bound.** An agent run is mostly waiting — on an LLM, on an external
  API, on the database. Very little CPU.
- **Tenant isolation is a security requirement**, not a feature. It has to be enforceable
  in one place and testable.
- **Both relational and vector data are needed.** Tenants, users, runs and audit logs are
  relational. RAG (M3) needs embeddings and similarity search.
- **Solo developer.** Every additional service is something I alone have to run, back up,
  secure and debug at 2am. Operational cost is a first-class constraint.

---

## Decision

### PostgreSQL 16 + pgvector — one database for both

Relational data *and* embeddings live in Postgres with the `pgvector` extension, rather
than Postgres plus a dedicated vector database (Pinecone, Weaviate, Qdrant).

**Why:** a tenant's documents and a tenant's rows are then in one database, under one
`tenant_id`, in one transaction, with one backup and one set of access controls. With a
separate vector store, tenant isolation must be enforced *twice*, in two different
systems, with two different filtering models — and any drift between them is a data leak.
A dedicated vector DB wins at very large scale; at this scale it buys performance I don't
need and doubles the surface I have to secure.

### Async end-to-end — FastAPI (ASGI) + SQLAlchemy 2.0 async + asyncpg

**Why:** during an agent run the process is idle, waiting on a network call. A blocking
stack holds a worker hostage for the whole run — concurrency then costs processes and
memory. Async lets one process hold thousands of in-flight runs. For an LLM platform this
is the difference between one server and twenty.

### Alembic for schema — never `create_all()`

**Why:** `create_all()` only creates tables that don't exist. It cannot alter, drop,
rename, backfill, or be undone, and it keeps no record of what has been applied. The day a
column is added, `create_all()` silently does nothing and production breaks. Alembic gives
ordered changes, a record (`alembic_version`), and a downgrade path. `alembic upgrade head`
is the deploy step in every environment.

### Redis for ephemeral state only

Rate-limit counters, job queues and caches. **Never the system of record** — if Redis is
wiped, nothing of value is lost.

### structlog with a bound request id

Every log line carries the request id, so one agent run is greppable end to end. JSON in
production, human-readable in development.

### src layout, package `eap`

Forces the installed package to be imported rather than the working directory, so tests
exercise what actually ships.

---

## Consequences

**Good**

- One database to run, back up, secure and migrate.
- Tenant isolation enforceable in one query layer, provable with one test.
- High concurrency on modest hardware.
- Schema changes are reviewable, ordered and reversible.
- CI can stand the whole stack up from two Docker images.

**Bad — accepted**

- **Async is harder.** A blocking call anywhere stalls the whole event loop. Every library
  must be async-compatible; `requests` and blocking DB drivers are banned.
- **pgvector is not a specialist.** Beyond roughly 1M vectors per tenant, a dedicated
  vector database would be faster. Accepted — that's a scaling problem worth having.
- **Migrations cost discipline.** Every model change needs a migration, reviewed. Slower
  than `create_all()`, deliberately.
- **Postgres is a single point of failure.** Both relational and vector reads depend on it.
  Mitigated by readiness probes and, later, a read replica.

---

## Revisit when

- A tenant exceeds ~1M vectors, or similarity search latency exceeds ~200ms → evaluate a
  dedicated vector store for that workload only.
- Agent runs routinely exceed request timeouts → move them to the Redis queue (M4).
- A second engineer joins → revisit whether async complexity is still worth the saving.