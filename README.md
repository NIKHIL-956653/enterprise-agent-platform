# Enterprise Agent Platform (`eap`)

A multi-tenant platform for running AI agents as a service — tenant isolation, an agent
runtime, per-tenant retrieval, and the operational plumbing (migrations, health probes,
rate limits, queues) that separates a demo from a product.

![CI](https://github.com/NIKHIL-956653/enterprise-agent-platform/actions/workflows/ci.yml/badge.svg)

---

## Why this exists

I previously built [GenAI-Document-Writing-Assistant](https://github.com/NIKHIL-956653/GenAI-Document-Writing-Assistant) —
a single agent that turns a Jira ticket into a drafted Google Doc / Confluence page.

It worked, and it made the real problem obvious: **one agent is a script, many agents for
many customers is a platform.** The hard parts aren't the prompts. They're tenant data
isolation, cost and rate control, schema evolution, observability, and not letting one
customer's workload affect another's.

`eap` is that platform. The document agent becomes its first tenant-facing workload.

## Architecture

| Layer | Choice | Reasoning |
|---|---|---|
| API | FastAPI (ASGI) | Async end-to-end — agent calls are I/O-bound, blocking the worker is the whole cost |
| Data | PostgreSQL 16 + pgvector | Relational data *and* embeddings in one engine; one database to operate, back up and secure |
| ORM | SQLAlchemy 2.0 async + asyncpg | Typed models, real async driver |
| Schema | Alembic | Ordered, reversible, recorded migrations — never `create_all()` |
| Cache / queues | Redis 7 | Rate-limit counters and job queues; deliberately not the system of record |
| Logging | structlog | Structured JSON in prod, with a request id bound to every line |
| Agents | LangGraph + Gemini | *(from M2)* |

Detailed rationale lives in [`docs/`](docs/) as ADRs.

## Run it

Requires Docker and Python 3.11+.

```bash
git clone https://github.com/NIKHIL-956653/enterprise-agent-platform.git
cd enterprise-agent-platform

cp .env.example .env           # defaults match docker-compose; no edits needed

python -m venv .venv
.venv/Scripts/activate         # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"

docker compose up -d           # Postgres + pgvector, Redis
alembic upgrade head           # build the schema
uvicorn eap.main:app --reload
```

Check it:

```bash
curl localhost:8000/livez     # {"status":"alive"}
curl localhost:8000/readyz    # {"status":"ready","checks":{"postgres":"ok","redis":"ok"}}
pytest
```

API docs at http://localhost:8000/docs.

## Running the UI

The admin console is a Vite + React + TypeScript app in `web/`. With the API running:

```bash
cd web
npm install
npm run dev            # http://localhost:5173
```

Seed the demo tenants first (idempotent, safe to re-run):

```bash
python scripts/seed_demo.py
```

| Tenant slug | Logins | Clearance |
|---|---|---|
| `acme` | `admin@acme.com` (owner) | 3 |
| `engg` | `admin@engg.edu` (admin) · `student@engg.edu` (member) | 3 · 1 |
| `mba` | `admin@mba.edu` (admin) · `student@mba.edu` (member) | 3 · 1 |
| `bba` | `admin@bba.edu` (admin) · `student@bba.edu` (member) | 3 · 1 |

Every password is `$DEMO_PASSWORD`, or the dev-only default `demo-password-123` when unset.
The three colleges are the isolation demo: one college cannot see another's documents, and a
student (clearance 1) cannot see faculty material classified 2 or 3, nor learn that it exists.
The dev server's origin (`localhost:5173`) is the one the API allows by default (EAP-51);
point the UI at a different API with `VITE_API_BASE`.

**Live tests.** The suite fakes every model call, so it is free and deterministic. To check
the real vendor as well, put a key in `.env` and run `pytest --live -m live`: four real runs
through the whole platform, a few cents on Claude Haiku 4.5. Without `--live` they are skipped.

## Layout