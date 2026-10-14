"""
Writing and verifying the audit trail.

record() is deliberately transport-agnostic: it takes a session and knows nothing about
runners, requests or workflows. When execution moves into Temporal (EAP-43), an activity
calls this exact function with its own session and nothing here changes - ADR-004's
sequencing constraint, honoured in the signature.

Note what record() does NOT send: seq, prev_hash, hash. The database trigger computes the
chain, so this module cannot write a row with a forged link even if it wanted to.

verify_chain() recomputes every link in SQL rather than Python. The hash covers
payload::text and created_at::text AS POSTGRES RENDERS THEM, and reproducing that
rendering byte-for-byte in Python is a standing bug invitation. Verifying where the data
lives means there is exactly one serialisation to agree on.
"""

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.audit.redact import redact


async def record(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    run_id: UUID | None = None,
) -> None:
    """Append one event to this tenant's trail. The trigger links it into the chain."""
    # Redaction happens HERE, not at call sites, so no caller can forget it. The count is
    # written into the payload: a trail that silently omits is a trail that lies, so the
    # fact of removal is itself on the record. The hash chains over the redacted form.
    cleaned, removed = redact(payload)
    if removed:
        cleaned["_redactions"] = removed
    await session.execute(
        text(
            "INSERT INTO audit_log (id, tenant_id, run_id, event_type, payload) "
            "VALUES (gen_random_uuid(), :tid, :rid, :etype, CAST(:payload AS jsonb))"
        ),
        {
            "tid": str(tenant_id),
            "rid": str(run_id) if run_id is not None else None,
            "etype": event_type,
            "payload": json.dumps(cleaned),
        },
    )


async def verify_chain(session: AsyncSession) -> int | None:
    """
    Recompute every link in the calling tenant's chain. RLS scopes the rows.

    Returns the seq of the first broken link, or None for an intact chain. Three breaks
    are detectable: an edited row (its hash no longer matches its content), a deleted row
    (the next row's prev_hash points at a hash no surviving row carries, and seq gaps),
    and a reordered row (same symptom). The one invisible edit is deleting the LAST row -
    a chain has no row after it to contradict. Closing that needs the head hash anchored
    outside the table; EAP-42 notes it, this ticket does not pretend otherwise.
    """
    broken = (
        await session.execute(
            text(
                "SELECT seq FROM ("
                "    SELECT seq, prev_hash, hash,"
                "           LAG(hash) OVER (ORDER BY seq) AS expected_prev,"
                "           LAG(seq) OVER (ORDER BY seq) AS expected_seq,"
                "           encode(digest("
                "               prev_hash || tenant_id::text || seq::text || event_type"
                "               || payload::text || COALESCE(run_id::text, '') || created_at::text,"
                "               'sha256'), 'hex') AS recomputed"
                "    FROM audit_log"
                ") links "
                "WHERE recomputed <> hash "
                "   OR prev_hash <> COALESCE(expected_prev, repeat('0', 64)) "
                "   OR seq <> COALESCE(expected_seq, 0) + 1 "
                "ORDER BY seq LIMIT 1"
            )
        )
    ).scalar_one_or_none()
    return broken
