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

    print("\n\n" + "=" * 50)
    print("UPCASTING & IMMUTABILITY VERIFIED")
    print("=" * 50)
    print(f"1. Stored v1 Event in DB (event_version = {raw_row['event_version']})")
    print(f"   Raw DB contains model_version? {'model_version' in raw_payload}")
    print(f"2. Loaded via EventStore (event_version = {loaded.event_version})")
    print(f"   Upcasted Payload contains model_version? {'model_version' in loaded.payload}")
    print(f"   Upcasted Payload contains confidence_score? {'confidence_score' in loaded.payload}")
    print(f"3. Re-queried DB for immutability (event_version = {raw_row_after['event_version']})")
    print(f"   Raw DB contains model_version? {'model_version' in raw_payload_after}")
    print("==================================================\n")

    await pool.close()


@pytest.mark.asyncio
async def test_decision_v1_reconstructs_model_versions_from_db() -> None:
    """DecisionGenerated v1 loaded from DB must have model_versions populated
    by reading the contributing AgentSession streams — not left as {}."""
    pool = await get_pool()
    store = EventStore(pool)

    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())
    session_stream = f"agent-{agent_id}-{session_id}"

    # Write the AgentSession stream with a known model_version
    await store.append(
        session_stream,
        [
            BaseEvent(
                event_type="AgentContextLoaded",
                payload={"agent_id": agent_id, "model_version": "v2.5"},
            )
        ],
        expected_version=-1,
    )

    # Write a v1 DecisionGenerated (no model_versions field)
    decision_stream = f"loan-decision-{uuid4()}"
    v1_decision = BaseEvent(
        event_type="DecisionGenerated",
        event_version=1,
        payload={
            "recommendation": "APPROVE",
            "contributing_agent_sessions": [session_stream],
            # deliberately no model_versions — this is a v1 event
        },
    )
    await store.append(decision_stream, [v1_decision], expected_version=-1)

    # Verify raw DB has no model_versions
    import json as _json

    raw = await pool.fetchrow(
        "SELECT payload, event_version FROM events WHERE stream_id = $1", decision_stream
    )
    assert raw is not None
    raw_payload = _json.loads(raw["payload"])
    assert "model_versions" not in raw_payload
    assert raw["event_version"] == 1

    # Load via EventStore — must reconstruct model_versions from the session stream
    events = await store.load_stream(decision_stream)
    assert len(events) == 1
    loaded = events[0]
    assert loaded.event_version == 2
    assert loaded.payload["model_versions"] == {session_stream: "v2.5"}

    # DB row must still be unchanged
    raw_after = await pool.fetchrow(
        "SELECT payload, event_version FROM events WHERE stream_id = $1", decision_stream
    )
    assert raw_after is not None
    assert _json.loads(raw_after["payload"]) == raw_payload
    assert raw_after["event_version"] == 1

    await pool.close()
