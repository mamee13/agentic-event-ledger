"""Integration test: cryptographic audit chain write + verify cycle."""

from uuid import uuid4

import pytest

from ledger.core.audit_chain import run_integrity_check, verify_chain
from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio
async def test_audit_chain_write_and_verify() -> None:
    pool = await get_pool()
    store = EventStore(pool)

    stream_id = f"audit-loan-{uuid4()}"

    # Write some domain events to the audit stream
    await store.append(
        stream_id,
        [
            BaseEvent(event_type="ApplicationSubmitted", payload={"application_id": "a1"}),
            BaseEvent(event_type="CreditAnalysisCompleted", payload={"risk_tier": "LOW"}),
        ],
        expected_version=-1,
    )

    # Run first integrity check
    h1, prev1 = await run_integrity_check(stream_id, store)
    assert prev1 == ""
    assert len(h1) == 64

    # Write more events
    v = await store.stream_version(stream_id)
    await store.append(
        stream_id,
        [BaseEvent(event_type="DecisionGenerated", payload={"recommendation": "APPROVE"})],
        expected_version=v,
    )

    # Run second integrity check — must chain from h1
    h2, prev2 = await run_integrity_check(stream_id, store)
    assert prev2 == h1
    assert h2 != h1

    # Verify the full chain is intact
    all_events = await store.load_stream(stream_id)
    check_runs = [e for e in all_events if e.event_type == "AuditIntegrityCheckRun"]
    assert len(check_runs) == 2

    ok, msg = verify_chain(check_runs, all_events)
    assert ok, msg

    await pool.close()


@pytest.mark.asyncio
async def test_audit_chain_detects_tampering() -> None:
    """verify_chain must fail if a stored hash doesn't match recomputed hash."""
    pool = await get_pool()
    store = EventStore(pool)

    stream_id = f"audit-loan-{uuid4()}"

    await store.append(
        stream_id,
        [BaseEvent(event_type="ApplicationSubmitted", payload={"application_id": "b1"})],
        expected_version=-1,
    )

    h1, _ = await run_integrity_check(stream_id, store)

    # Load events and simulate tampering by injecting a wrong hash into the check run
    all_events = await store.load_stream(stream_id)
    check_runs = [e for e in all_events if e.event_type == "AuditIntegrityCheckRun"]

    # Build a fake check run with a wrong integrity_hash
    from datetime import UTC, datetime

    from ledger.core.models import StoredEvent

    tampered_check = StoredEvent(
        event_id=check_runs[0].event_id,
        event_type="AuditIntegrityCheckRun",
        payload={"integrity_hash": "deadbeef" * 8, "previous_hash": ""},
        stream_id=stream_id,
        stream_position=check_runs[0].stream_position,
        global_position=check_runs[0].global_position,
        recorded_at=datetime.now(UTC),
    )

    data_events = [e for e in all_events if e.event_type != "AuditIntegrityCheckRun"]
    ok, msg = verify_chain([tampered_check], data_events + [tampered_check])
    assert not ok
    assert "mismatch" in msg.lower()

    await pool.close()
