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
    pool = await get_pool()
    store = EventStore(pool)

    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())
    stream_id = f"agent-{agent_id}-{session_id}"
    app_id = str(uuid4())

    # Simulate a session that crashed mid-analysis:
    # AgentContextLoaded → CreditAnalysisCompleted → CreditAnalysisRequested (crash here)
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
            event_type="CreditAnalysisRequested",
            payload={"application_id": f"loan-{uuid4()}"},
        ),
    ]
    await store.append(stream_id, events, expected_version=-1)

    # Reconstruct context
    ctx = await reconstruct_agent_context(stream_id, store)

    # Agent can identify itself
    assert ctx.agent_id == agent_id
    assert ctx.model_version == "v2.0"
    assert ctx.session_id == stream_id

    # Crash state is correctly flagged
    assert ctx.needs_reconciliation is True
    assert ctx.pending_work == "CreditAnalysisRequested"
    assert ctx.last_completed_action == "CreditAnalysisCompleted"

    # Session is still considered active (no SessionTerminated)
    assert ctx.is_active is True

    # Enough history to resume: total events known
    assert ctx.total_events == 3

    # Recent events are verbatim (last 3 = all 3 in this case)
    assert len(ctx.recent_events) == 3
    assert ctx.recent_events[0]["event_type"] == "AgentContextLoaded"
    assert ctx.recent_events[2]["event_type"] == "CreditAnalysisRequested"

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
