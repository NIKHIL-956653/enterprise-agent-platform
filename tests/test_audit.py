"""
The audit trail's three claims, each with a test that tries to break it.

1. Rows chain: every row's hash covers the previous one, back to a zero genesis.
2. eap_app cannot rewrite history: UPDATE and DELETE are revoked at the grant level.
3. Owner-level tampering is DETECTABLE: edit or delete a row as the owner - the role the
   revoke cannot stop - and verify_chain names the first broken link.

Detectable, not impossible, is the claim. The known hole is stated where it lives: see
verify_chain's docstring on deleting the last row.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from eap.audit import record, verify_chain
from eap.core.db import get_owner_session_factory, get_session_factory

GENESIS = "0" * 64


async def _seed_tenant() -> str:
    slug = f"t-{uuid.uuid4().hex[:8]}"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name) "
                    "VALUES (gen_random_uuid(), :slug, :slug) RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar_one()
        await s.commit()
    return str(tid)


async def _record(tenant_id: str, event_type: str, payload: dict) -> None:
    """One event through the same path a request takes: eap_app role, tenant context, RLS."""
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        await record(s, tenant_id=uuid.UUID(tenant_id), event_type=event_type, payload=payload)
        await s.commit()


async def _rows(tenant_id: str) -> list:
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        return (await s.execute(text("SELECT seq, prev_hash, hash FROM audit_log ORDER BY seq"))).all()


async def _verify(tenant_id: str) -> int | None:
    async with get_session_factory()() as s:
        await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id})
        return await verify_chain(s)


async def test_rows_chain_from_a_zero_genesis() -> None:
    tid = await _seed_tenant()
    for i in range(3):
        await _record(tid, "test.event", {"n": i})

    rows = await _rows(tid)

    assert [r.seq for r in rows] == [1, 2, 3]
    assert rows[0].prev_hash == GENESIS
    assert rows[1].prev_hash == rows[0].hash
    assert rows[2].prev_hash == rows[1].hash
    assert len({r.hash for r in rows}) == 3  # all distinct


async def test_an_untouched_chain_verifies() -> None:
    tid = await _seed_tenant()
    for i in range(5):
        await _record(tid, "test.event", {"n": i})

    assert await _verify(tid) is None


async def test_editing_a_payload_is_detected() -> None:
    tid = await _seed_tenant()
    for i in range(3):
        await _record(tid, "test.event", {"n": i})

    # The owner role is the one the REVOKE cannot stop. This is the attacker model: not an
    # outsider, but us - or anyone holding the owner credentials - editing history.
    async with get_owner_session_factory()() as s:
        await s.execute(
            text("UPDATE audit_log SET payload = '{\"n\": 999}'::jsonb WHERE seq = 2 AND tenant_id = :tid"),
            {"tid": tid},
        )
        await s.commit()

    assert await _verify(tid) == 2


async def test_deleting_a_middle_row_is_detected() -> None:
    tid = await _seed_tenant()
    for i in range(3):
        await _record(tid, "test.event", {"n": i})

    async with get_owner_session_factory()() as s:
        await s.execute(text("DELETE FROM audit_log WHERE seq = 2 AND tenant_id = :tid"), {"tid": tid})
        await s.commit()

    # Row 3's prev_hash points at a hash no surviving row carries, and seq jumps 1 -> 3.
    assert await _verify(tid) == 3


async def test_the_app_role_cannot_update_or_delete() -> None:
    tid = await _seed_tenant()
    await _record(tid, "test.event", {"n": 0})

    # Refused by the grant, not by convention. Separate sessions: the first failure
    # poisons its transaction.
    with pytest.raises(DBAPIError):
        async with get_session_factory()() as s:
            await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tid})
            await s.execute(text("UPDATE audit_log SET event_type = 'forged' WHERE seq = 1"))

    with pytest.raises(DBAPIError):
        async with get_session_factory()() as s:
            await s.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tid})
            await s.execute(text("DELETE FROM audit_log WHERE seq = 1"))


async def test_chains_are_independent_per_tenant() -> None:
    tid_a = await _seed_tenant()
    tid_b = await _seed_tenant()
    await _record(tid_a, "test.event", {"who": "a"})
    await _record(tid_a, "test.event", {"who": "a"})
    await _record(tid_b, "test.event", {"who": "b"})

    # B's chain starts at its own genesis, not at A's seq 3.
    rows_b = await _rows(tid_b)
    assert [r.seq for r in rows_b] == [1]
    assert rows_b[0].prev_hash == GENESIS

    # Tampering with A must not implicate B's trail.
    async with get_owner_session_factory()() as s:
        await s.execute(text("DELETE FROM audit_log WHERE tenant_id = :tid AND seq = 1"), {"tid": tid_a})
        await s.commit()

    assert await _verify(tid_a) == 2
    assert await _verify(tid_b) is None
