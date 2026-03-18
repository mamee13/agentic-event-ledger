"""Integration test: upcasting immutability.

Verifies that loading a v1 event through EventStore returns a v2-shaped payload
while the raw DB row remains unchanged (upcasting is in-memory only).
"""

from uuid import uuid4

import pytest

from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio
async def test_upcasting_does_not_mutate_db_row() -> None:
    pool = await get_pool()
    store = EventStore(pool)

    stream_id = f"agent-immutability-{uuid4()}"

    # Write a v1 CreditAnalysisCompleted — no model_version, no confidence_score
    v1_event = BaseEvent(
        event_type="CreditAnalysisCompleted",
        event_version=1,
        payload={"application_id": "loan-test", "risk_tier": "LOW"},
    )
    await store.append(stream_id, [v1_event], expected_version=-1)

    # 1. Query raw DB payload — must NOT have model_version yet
    raw_row = await pool.fetchrow(
        "SELECT payload, event_version FROM events WHERE stream_id = $1", stream_id
    )
    assert raw_row is not None
    import json

    raw_payload = json.loads(raw_row["payload"])
    assert "model_version" not in raw_payload, "DB row must not be modified before load"
    assert raw_row["event_version"] == 1

    # 2. Load via EventStore — upcaster must add v2 fields
    events = await store.load_stream(stream_id)
    assert len(events) == 1
    loaded = events[0]
    assert loaded.event_version == 2, "EventStore must return upcasted version"
    assert "model_version" in loaded.payload, "model_version must be present after upcasting"
    assert "confidence_score" in loaded.payload, "confidence_score must be present after upcasting"
    assert "regulatory_basis" in loaded.payload, "regulatory_basis must be present after upcasting"
    # confidence_score must be null for v1 events
    assert loaded.payload["confidence_score"] is None

    # 3. Re-query raw DB — must still be v1 and unchanged
    raw_row_after = await pool.fetchrow(
        "SELECT payload, event_version FROM events WHERE stream_id = $1", stream_id
    )
    assert raw_row_after is not None
    raw_payload_after = json.loads(raw_row_after["payload"])
    assert "model_version" not in raw_payload_after, "DB row must remain unchanged after load"
    assert raw_row_after["event_version"] == 1, "DB event_version must remain 1"

    await pool.close()
