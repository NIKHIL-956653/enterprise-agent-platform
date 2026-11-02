# ADR-006 — `external_id` uniqueness per classification

**Status:** accepted · **Date:** 2026-11-01 · **Ticket:** EAP-61 · **Migration:** `0012`

(ADR-005 is reserved for "i18n as data, not branches", EAP-L6.)

## Context

`documents` was unique on `(tenant_id, external_id)`, across classifications. Ingest is an
`INSERT ... ON CONFLICT DO UPDATE` on that key. When a caller posted an `external_id` that
already existed **above their clearance**, the conflict landed on a row RLS hides from them,
Postgres refused the `DO UPDATE` under the policy, and the handler turned that into
`403 classification exceeds your clearance`. A free id got `201`.

Reproduced on the demo seed: `admin@engg.edu` ingests `exam-key` at classification 3;
`student@engg.edu` (clearance 1) lists documents and sees nothing; the student posts their
own document as `exam-key` and gets 403, as `not-taken` and gets 201. Existence only, never
content, and only to an authenticated member of the tenant — but ids from
`scripts/ingest_pdfs.py` are file names, which are guessable, and the demo's claim is that a
student is *never told it exists*.

The requirement: no response, status code, body or timing may differ between "exists but
invisible to the caller" and "does not exist".

## Options

1. **Scope uniqueness to `(tenant_id, classification, external_id)`.** A student's
   `exam-key` at 1 and an admin's at 3 coexist. A re-post only ever conflicts with a row at
   the classification being written, which the caller can see, so the RLS-blocked update
   never happens. Consequence: one `external_id` can exist at several classifications.
2. **Option 1 plus consolidation of visible copies.** Before the upsert, delete every
   document the caller *can see* with the same `external_id` at a different classification.
   Re-posting at a new level then moves the document rather than duplicating it, which is
   what "re-posting replaces" meant before; copies above the caller's clearance are
   untouched and unmentioned. The delete runs on every explicit-id ingest whether or not a
   copy exists, so nothing in the request's behaviour depends on invisible rows.
3. **Keep the key; return 409 for every conflict.** Same oracle, different number.
4. **Keep the key; make probing detectable** (audit `document.write_refused`, rate-limit
   refused writes). Leaves the leak and adds machinery around it.
5. **Keep the key; answer 201 and silently do nothing** when the conflict is invisible.
   A lie, and still distinguishable: the caller's listing would not show their document.
6. **Namespace ids per user or per clearance level** (`3:exam-key`). Closes the oracle but
   breaks the contract that an id is the caller's own name for a source, and the ingest
   script's "file name = id" with it.

## Decision

Option 2. Option 1 closes the oracle on its own; the consolidation step is what keeps the
existing promise — *re-posting the same `external_id` replaces the document, not adds to
it* — true for every document the caller can see, including across a classification change.
Without it, an admin reclassifying a syllabus from 1 to 3 by re-posting would leave the
public copy in place, which is the opposite of what they asked for.

Why it leaks nothing: every statement an explicit-id ingest runs — the delete of other-level
copies, the upsert, the chunk replacement — is scoped by RLS the same way, executes
unconditionally, and touches only rows at or below the caller's clearance. The response
(`201`, `{document_id, external_id, chunk_count}`) is built from the caller's own row. The
only difference between "a hidden copy exists" and "none exists" is one index entry
Postgres reads and discards under the policy, behind an embedding call measured in
hundreds of milliseconds. There is no branch in the code on either side of the database
that can see the hidden row.

Why not 3–6: 3 and 4 keep the oracle; 5 lies; 6 trades a semantics change for a bigger one.

## Consequences

- **One `external_id` can exist at several classifications** within a tenant, whenever a
  lower-clearance caller posts an id a higher level already uses. A high-clearance user
  listing documents may see two rows with the same id. Re-posting as a user who can see all
  copies consolidates them. This is the price of the oracle closing, and it is recorded here
  rather than hidden.
- **Reclassification by re-post works as before for whoever can see the document**, and
  never for a copy they cannot see.
- **Migration `0012` is reversible only while no tenant holds an id at two levels.** The
  downgrade recreates the old constraint and fails loudly on a unique violation rather than
  deleting anyone's documents to make the old invariant true; consolidate first.
- **`scripts/ingest_pdfs.py` is unchanged in code.** It posts file names as explicit ids at
  one classification per run; re-running at the same level replaces, re-running at another
  level moves the document for an operator who can see it (an admin can). The docstring says
  so.
- **The 403 remains** for the one case it was written for: posting a document above your own
  clearance, refused by the policy's `WITH CHECK`.
- **The auto-generated ids from D4** (`slug-<8 hex>`, `ON CONFLICT DO NOTHING` with retry)
  never touched this path and are unaffected.

## Tests

`tests/test_external_id_oracle.py`: a student probing a restricted id and a free id gets
responses identical in status, headers (apart from `date`) and body (apart from the
generated `document_id` and the id they posted); their own listing shows exactly their two
documents; the admin's restricted copy is untouched. Re-posting their own document replaces
it, not duplicates it, with the restricted copy still untouched. An admin re-posting at a
new classification consolidates the copies they can see, and the student then sees nothing.
