"""Integration test: crash recovery via reconstruct_agent_context.

Proves that after a simulated crash (session left in partial state),
reconstruct_agent_context returns enough information for the agent to resume.
"""

from uuid import uuid4

import pytest

from ledger.core.agent_context import reconstruct_agent_context
from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio
async def test_crash_recovery_reconstruction() -> None:
    """Append 5 events, discard in-memory agent, reconstruct cold.

    Assertions:
    - pending_work is non-empty (last event is a partial/in-flight type)
    - last_event_position matches the fifth event's stream_position
    """
    pool = await get_pool()
    store = EventStore(pool)

    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())
    stream_id = f"agent-{agent_id}-{session_id}"
    app_id = str(uuid4())
    app_id2 = str(uuid4())

    # 5 events: context load → two completed analyses → one more completed → crash mid-request
    events = [
        BaseEvent(
            event_type="AgentContextLoaded",
            payload={"agent_id": agent_id, "model_version": "v2.0", "context_token_count": 512},
        ),
        BaseEvent(
            event_type="CreditAnalysisCompleted",
            payload={"application_id": app_id, "risk_tier": "LOW"},
        ),
        BaseEvent(
            event_type="FraudScreeningCompleted",
            payload={"application_id": app_id, "fraud_score": 0.1},
        ),
        BaseEvent(
            event_type="CreditAnalysisCompleted",
            payload={"application_id": app_id2, "risk_tier": "MEDIUM"},
        ),
        # Crash here — request was in-flight, no matching Completed
        BaseEvent(
            event_type="CreditAnalysisRequested",
            payload={"application_id": f"loan-{uuid4()}"},
        ),
    ]
    await store.append(stream_id, events, expected_version=-1)

    # Discard in-memory agent — reconstruct cold from DB
    ctx = await reconstruct_agent_context(stream_id, store)

    # Agent can identify itself
    assert ctx.agent_id == agent_id
    assert ctx.model_version == "v2.0"
    assert ctx.session_id == stream_id

    # Crash state is correctly flagged
    assert ctx.needs_reconciliation is True
    assert ctx.pending_work, "pending_work must be non-empty after crash"
    assert ctx.pending_work == ["CreditAnalysisRequested"]
    assert ctx.session_health_status == "NEEDS_RECONCILIATION"

    # last_event_position must match the fifth event's stream_position (= 5)
    loaded_events = await store.load_stream(stream_id)
    fifth_event = loaded_events[4]
    assert ctx.total_events == 5
    assert fifth_event.stream_position == 5
    assert ctx.last_event_position == fifth_event.stream_position

    # Session is still considered active (no SessionTerminated)
    assert ctx.is_active is True

    print("\n\n" + "=" * 50)
    print("GAS TOWN RECOVERY: AGENT CONTEXT RECONSTRUCTED")
    print("=" * 50)
    print(f"Agent ID:       {ctx.agent_id}")
    print(f"Model Version:  {ctx.model_version}")
    print(f"Total Events:   {ctx.total_events}")
    print(f"Health Status:  {ctx.session_health_status}")
    print(f"Needs Recon?    {ctx.needs_reconciliation}")
    print(f"Pending Work:   {ctx.pending_work}")
    print("Agent has successfully resumed its context from the event stream after crash.")
    print("==================================================\n")

    await pool.close()


@pytest.mark.asyncio
async def test_completed_session_no_reconciliation_needed() -> None:
    pool = await get_pool()
    store = EventStore(pool)

    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())
    stream_id = f"agent-{agent_id}-{session_id}"

    events = [
        BaseEvent(
            event_type="AgentContextLoaded",
            payload={"agent_id": agent_id, "model_version": "v2.0"},
        ),
        BaseEvent(
            event_type="CreditAnalysisCompleted",
            payload={"application_id": "loan-done"},
        ),
        BaseEvent(event_type="SessionTerminated", payload={}),
    ]
    await store.append(stream_id, events, expected_version=-1)

    ctx = await reconstruct_agent_context(stream_id, store)

    assert ctx.needs_reconciliation is False
    assert ctx.is_active is False
    assert ctx.last_completed_action == "SessionTerminated"

    await pool.close()
