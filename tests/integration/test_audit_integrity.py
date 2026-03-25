"""Integration test: cryptographic audit chain write + verify cycle."""

import json
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
    result1 = await run_integrity_check(stream_id, store)
    assert result1.previous_hash == ""
    assert len(result1.integrity_hash) == 64
    assert result1.chain_valid is True
    assert result1.tamper_detected is False

    # Write more events
    v = await store.stream_version(stream_id)
    await store.append(
        stream_id,
        [BaseEvent(event_type="DecisionGenerated", payload={"recommendation": "APPROVE"})],
        expected_version=v,
    )

    # Run second integrity check — must chain from result1
    result2 = await run_integrity_check(stream_id, store)
    assert result2.previous_hash == result1.integrity_hash
    assert result2.integrity_hash != result1.integrity_hash
    assert result2.chain_valid is True
    assert result2.tamper_detected is False

    # Verify the full chain is intact via verify_chain directly
    all_events_raw = await store.load_stream_raw(stream_id)
    check_runs = [e for e in all_events_raw if e.event_type == "AuditIntegrityCheckRun"]
    assert len(check_runs) == 2

    ok, msg = verify_chain(check_runs, all_events_raw)
    assert ok, msg

    await pool.close()


@pytest.mark.asyncio
async def test_audit_chain_detects_tampering() -> None:
    """Tamper detection: modify a stored event payload directly in the DB,
    then re-run run_integrity_check and assert tamper_detected = True."""
    pool = await get_pool()
    store = EventStore(pool)

    stream_id = f"audit-loan-{uuid4()}"

    # Write a domain event and run the first integrity check to seal the chain
    await store.append(
        stream_id,
        [BaseEvent(event_type="ApplicationSubmitted", payload={"application_id": "b1"})],
        expected_version=-1,
    )
    result1 = await run_integrity_check(stream_id, store)
    assert result1.chain_valid is True

    # --- Direct DB tampering ---
    # Overwrite the payload of the ApplicationSubmitted event in the DB.
    # This simulates an attacker modifying a stored event after the chain was sealed.
    await pool.execute(
        """
        UPDATE events
        SET payload = $1
        WHERE stream_id = $2
          AND event_type = 'ApplicationSubmitted'
        """,
        json.dumps({"application_id": "TAMPERED"}),
        stream_id,
    )

    # Re-run the integrity check — the new hash will not match the stored one
    result2 = await run_integrity_check(stream_id, store)
    assert result2.tamper_detected is True, (
        "tamper_detected must be True after direct DB payload modification"
    )
    assert result2.chain_valid is False

    await pool.close()
