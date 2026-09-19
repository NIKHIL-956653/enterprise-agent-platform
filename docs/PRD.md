# PRD — Enterprise Agent Platform (eap)

**Status:** Draft v1 · **Owner:** Nikhil Vijayapuri · **Last updated:** Sprint 0

---

## 1. Problem

Teams that build one useful AI agent quickly want a second, then a fifth. Each new agent
re-solves the same problems: where do prompts and config live, how is tenant data kept
separate, how are LLM costs controlled per customer, how does anyone know what an agent
did and why.

The first agent (a Jira-ticket → drafted-document assistant) proved the value. It also
proved that *one agent is a script; many agents for many customers is a platform.*

## 2. Goal

A multi-tenant platform that runs many AI agents for many organisations, where tenant
isolation, cost control, observability and schema evolution are solved **once**, in the
platform, not per agent.

## 3. Users

| User | What they need |
|---|---|
| **Tenant admin** | Create their org, manage users, see what agents ran and what it cost |
| **Tenant user** | Trigger an agent, see the result, trust that nobody else sees their data |
| **Platform operator** (me) | Deploy, migrate, observe, rate-limit, without touching tenant data |
| **Agent developer** | Add a new agent by writing the agent, not the plumbing |

## 4. Scope by milestone

| Milestone | Delivers | User-visible outcome |
|---|---|---|
| **M0** | Skeleton: config, async DB, Redis, migrations, health probes, CI | Platform boots, proves itself healthy, tests run in CI |
| **M1** | Tenants + auth: JWT, users, `tenant_id` isolation on every query | Two orgs can exist and cannot see each other's data |
| **M2** | Agent runtime: LangGraph + Gemini, the document agent ported as agent #1 | A tenant user triggers an agent and gets a result |
| **M3** | Per-tenant RAG on pgvector | Agents answer from the tenant's own documents |
| **M4** | Rate limits + job queues on Redis | One tenant's load cannot degrade another's; long runs don't block requests |
| **M5** | React admin UI | Tenant admin manages users, views runs and cost without an API client |

## 5. Non-goals (v1)

- Multi-region or multi-cloud deployment
- Billing / payments integration (cost is *tracked*, not *charged*)
- Model training or fine-tuning — inference only
- A marketplace of third-party agents
- SSO / SAML — email + password and API keys only
- Real-time streaming of agent output (request → result is enough for v1)

## 6. Success criteria

- A new agent can be added without changing any platform code outside its own module.
- An automated test proves tenant A cannot read tenant B's data through any endpoint.
- Every agent run is attributable: tenant, user, agent, tokens used, latency, outcome.
- `alembic upgrade head` + `pytest` are green in CI on every merge to `main`.
- The platform survives a Postgres or Redis outage without restarting pods
  (readiness fails, liveness holds).

## 7. Key risks

| Risk | Mitigation |
|---|---|
| Tenant data leak via a missed `tenant_id` filter | Isolation enforced in one place (a scoped session / query layer), covered by a test that actively tries to cross tenants |
| LLM cost runaway | Per-tenant token budgets and rate limits (M4); every call logged with token counts |
| Agent code coupling to platform internals | Agents get a narrow interface: input, tenant context, tools; nothing else |
| Solo-developer bus factor | Everything documented: ADRs, runbook, backlog, this PRD |

## 8. Out of scope questions, parked

- Should agents be able to call other agents? (Decide in M2 once the runtime exists.)
- Do we need per-tenant model selection? (Decide in M3 when RAG makes cost visible.)