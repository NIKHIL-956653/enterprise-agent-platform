# ADR-004 — Enterprise hardening: authorization, audit, durable execution

**Status:** Proposed · **Date:** Sprint 4 planning · **Decider:** Nikhil Vijayapuri

---

## Context

Three things gate any serious enterprise deployment and none of them exist today: per-user
authorization inside a tenant, a tamper-evident audit trail, and durable execution. This ADR
records the decisions up front so the sprint implements a design rather than discovers one
mid-build.

These are **proposed**, not accepted. A scoping workshop may change them, and the status
line moves to Accepted when the first ticket merges.

## Decision 1 — Authorization is enforced at retrieval, never at handoff

Today a tenant is flat: RLS scopes on `tenant_id` alone, so every user of a tenant can
reach every document that tenant owns. That is wrong for an organisation where an
executive's documents and an intern's sit in the same corpus.

The obvious-looking fix — check permissions when one agent hands context to another — is
the wrong layer. Once text is inside a prompt it is ungoverned: it can be summarised,
paraphrased, or written into a scratchpad, and no downstream check can un-see it.

**Decision:** classification lives on the document, the principal travels with the request,
and the RLS predicate reads both. A chunk the caller is not cleared for is never retrieved,
so it never enters a prompt, so it cannot leak through one.

Concretely: a `classification` column on `documents`, propagated to `document_chunks` for
the same reason `tenant_id` is (ADR-002 §RLS cannot follow foreign keys). The request sets
`app.user_id` and `app.clearance` alongside `app.tenant_id`, from the verified token only.

Two rules follow, and both need stating because neither is enforced by the database:

1. **A handoff narrows, never widens.** A delegated agent runs with the intersection of its
   own grant and its caller's. Union is how privilege escalation gets built by accident.
2. **Context inherits the maximum classification of its sources.** A scratchpad built from
   a restricted document is restricted, whatever it now says.


## Decision 2 — The audit trail is a separate, append-only, hash-chained table

`agent_runs` records what happened: input, output, status, error, tokens, latency. That is
a run record and it is enough to debug. It is not an audit trail, for two reasons. It omits
what a reviewer needs — the rendered prompt, which chunks retrieval chose, each step of a
graph, raw completions, tool arguments. And it is mutable by the application role, so it
proves nothing to anyone who does not already trust us.

| Option | Tamper-evidence | Cost |
|---|---|---|
| Keep extending `agent_runs` | None — the app can UPDATE it | Free |
| Separate table, INSERT-only grant, hash-chained rows | **Edits and deletions are detectable** | One table, one role, modest write volume |
| External append-only store (S3 Object Lock, QLDB) | Strongest | A second system, a second failure mode |

**Decision:** the middle option. A dedicated table whose writer role is granted `INSERT`
and nothing else — no `UPDATE`, no `DELETE` — enforced by Postgres the same way RLS is, so
the guarantee does not depend on application discipline. Each row carries the hash of the
previous row, making a deletion or an edit detectable rather than invisible.

Captured per step: rendered prompt, retrieved chunk ids with similarity scores, model and
version, raw completion, tool name, arguments and result.

**Written transport-agnostic.** Decision 3 moves execution into workflows; audit capture
points coupled to today's runner would be rebuilt. The capture interface must not know
whether it is called from a request or an activity.

PII is redacted at write against a documented policy, classified at ingest, and **the fact
of redaction is itself logged** — a trail that silently omits is a trail that lies.

Open: retention period and who may read the trail. Both are the customer's compliance
framework to decide, not ours to guess.



## Decision 3 — Durable execution on Temporal, with step-level checkpoints

Runs execute inside the HTTP request on asyncio. A slow model call is an `await`, so it
holds no thread — the real exhaustion risk is the connection pool, not the worker pool.
Today's only protections are a per-run timeout (recorded as `TIMED_OUT`, not a hang) and
per-tenant rate limiting (EAP-46). There are no retries, no circuit breaker, no idempotency.

| Option | Partial state recovery | Operational cost |
|---|---|---|
| Celery / RQ / BullMQ | Hand-rolled — you rebuild the state machine | Low |
| Temporal | **Built in — durable execution and replay** | A cluster, or Temporal Cloud |
| Stay in-request | None | Zero, until the first incident |

**Decision:** Temporal. The deciding property is not queueing — any of these queue. It is
that a multi-step LLM loop (outline → draft → review) has expensive partial state, and
Temporal's durable execution recovers it without us writing a recovery protocol. With
Celery we would build that badly and own it forever.

Not charging twice for the same work has two halves. **Checkpoint after each step**, so a
retry resumes from the last completed one instead of re-running the chain — LangGraph's
checkpointer does this. And **an idempotency key on the request**, so a client retry does
not start a second run at all.

Upstream failures: a token bucket per provider on egress, exponential backoff with jitter,
and a circuit breaker that fails fast while open. The mistake to avoid is queueing work
behind a provider that is already down — that converts a vendor blip into a backlog.

A loop guard is a **maximum step count in the graph**, not only a wall clock. An infinite
loop can be fast.

The run endpoint already returns a run id and a status, shaped so it can return `queued`
without any caller changing. That was deliberate (EAP-20) and it pays off here.

## Consequences

- Decision 1 touches every tenant-owned table's policy. The EAP-35 isolation tests must
  keep passing unchanged, and gain siblings: a cleared user retrieves, an uncleared one
  gets `[]` for the same query.
- Decision 2 adds a write per step. Audit volume will exceed run volume by roughly the
  average step count — a real retention cost, not a rounding error.
- Decision 3 adds infrastructure we have never operated. The estimate is wide on purpose;
  a first Temporal deployment reliably costs more than planned.
- All three are sequenced by what blocks a pilot, not by what is most visible. A deployment
  that blocks on something else reorders the sprint, not the decisions.

