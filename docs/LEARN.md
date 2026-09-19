Concepts I've hit while building eap, in my own words. Not documentation — the stuff I want to be able to explain out loud.

M0 — Skeleton
Postgres

The database. A server that stores data on disk and answers SQL. Nothing my app holds in memory survives a restart; anything that matters lives here. Chose it over MySQL/Mongo mainly for pgvector — an extension that stores embeddings and does similarity search. Means relational data and vectors live in one database instead of two. One thing to run, one thing to secure.

Redis

Fast, in-memory, forgets things. Opposite of Postgres. For rate-limit counters and job queues. Wrong tool for anything I can't afford to lose.

ASGI

The agreed plug shape between a web server and a Python app. WSGI (the old one) was one request at a time, blocking. ASGI is async — my app can await a slow LLM call and serve other requests while it waits. FastAPI speaks ASGI; uvicorn runs it.

ASGITransport in my tests skips uvicorn entirely — httpx pretends to be the server and hands the request straight into the app, in-process. No port, no boot wait, but still the real routes, real middleware, real DB.

Alembic

Version control for the database schema. Each change is a numbered file; Alembic keeps a row in alembic_version saying which one is applied. upgrade head applies what's missing, downgrade -1 undoes.

Why not create_all(): it only creates tables that don't exist. Add a column and it silently does nothing — code expects a column the DB doesn't have, 500 in production. No alter, no undo, no record of what ran. create_all is for a scratch database; migrations are for one with data in it.

Liveness vs readiness
/livez — "is this process wedged? restart it." Touches nothing.
/readyz — "should I get traffic right now?" Checks Postgres and Redis.

If liveness checked the database, a 30-second Postgres blip would make Kubernetes restart every pod at once — they all share the DB, so they all fail together. Then Postgres comes back and gets hammered by a fleet of cold-starting pods. A restart loop caused purely by a monitoring choice. Readiness failing only removes the pod from the load balancer. No restart, recovers on its own.

Two bugs I found in my own health checks
EAP-9 — /readyz says "degraded" in the body but still returns HTTP 200. Load balancers read the status code, not the body. So with Postgres down, the pod keeps getting traffic it can't serve.
EAP-10 — /readyz checks out a real pooled connection every call. Under load all connections are busy, the probe blocks, times out, and the orchestrator pulls a pod that was only busy. Its traffic moves to the others, which saturate faster and fail too. Cascading outage caused by the monitoring, not the fault.

The rule both bugs teach: monitoring must be cheaper than the thing it monitors.

CI

GitHub Actions runs ci.yml on every push: fresh Ubuntu, starts Postgres + Redis, installs the project, runs ruff, applies migrations, runs pytest. Green means it works on a machine that isn't mine — not "works on my laptop".

Line endings

Windows writes CRLF, Linux LF. Ruff's default keeps whatever each file has, so files drifted and CI (Ubuntu) could disagree with my laptop. Fixed in two places: line-ending = "lf" in pyproject (my disk) and * text=auto eol=lf in .gitattributes (the repo).

Git flow

Ticket → branch (feat/eap-N-name) → commits referencing the ticket → push → PR → CI runs on the PR → merge → delete branch. main is protected: no direct pushes, CI must be green. Every commit says Refs EAP-N, so git blame on any line leads to the ticket that explains why.

M1 — Tenants & auth
Pooled vs silo multi-tenancy
Pooled — everyone shares tables, each row carries tenant_id.
Silo — each tenant gets their own schema or database.

Real SaaS runs pooled by default and offers silo as an upsell to customers who demand physical separation. Nobody runs silo for everyone — migrations across 500 schemas is a permanent tax.

Row-Level Security (RLS)

Postgres enforces the tenant filter itself. Each request sets a session variable (app.tenant_id) from the verified JWT; RLS policies restrict every read and write to matching rows.

The point: a query that forgets WHERE tenant_id = ... returns zero rows, not another tenant's data. The guarantee sits below my code, so it holds when my code is wrong. App-level filtering only is as strong as the least careful line anyone ever writes.

Two sharp edges:

Connection pooling. The session variable must be set at the start of every request and cleared at the end. A recycled connection carrying a stale tenant id is the one way this design leaks.
Table owners bypass RLS. The app must run as a restricted role, not the role that owns the tables, or every policy is silently off.
Where tenant_id comes from

Only the verified token. Never a request body, query string or header. If a caller can name the tenant they want, there is no isolation.