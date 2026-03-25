"""Unit tests for reconstruct_agent_context."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ledger.core.agent_context import reconstruct_agent_context
from ledger.core.models import StoredEvent


def _stored(
    event_type: str,
    payload: dict,  # type: ignore[type-arg]
    pos: int,
) -> StoredEvent:
    return StoredEvent(
        event_id=uuid4(),
        event_type=event_type,
        payload=payload,
        stream_id="agent-a1-s1",
        stream_position=pos,
        global_position=pos,
        recorded_at=datetime.now(UTC),
    )


def _mock_store(events: list[StoredEvent]) -> AsyncMock:
    store = AsyncMock()
    store.load_stream = AsyncMock(return_value=events)
    return store


# ---------------------------------------------------------------------------
# Empty stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_stream_returns_safe_defaults() -> None:
    ctx = await reconstruct_agent_context("agent-a1-s1", _mock_store([]))
    assert ctx.total_events == 0
    assert ctx.needs_reconciliation is False
    assert ctx.agent_id is None
    assert ctx.recent_events == []
    assert ctx.pending_work == []
    assert ctx.session_health_status == "OK"
    assert ctx.last_event_position == 0


# ---------------------------------------------------------------------------
# Normal completed session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_session_no_reconciliation() -> None:
    events = [
        _stored("AgentContextLoaded", {"agent_id": "a1", "model_version": "v2"}, 1),
        _stored("CreditAnalysisCompleted", {"application_id": "loan-1"}, 2),
        _stored("DecisionGenerated", {"application_id": "loan-1"}, 3),
        _stored("SessionTerminated", {}, 4),
    ]
    ctx = await reconstruct_agent_context("agent-a1-s1", _mock_store(events))
    assert ctx.needs_reconciliation is False
    assert ctx.is_active is False
    assert ctx.last_completed_action == "SessionTerminated"
    assert ctx.agent_id == "a1"
    assert ctx.model_version == "v2"
    assert ctx.total_events == 4


# ---------------------------------------------------------------------------
# Partial / in-flight session → NEEDS_RECONCILIATION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_last_event_sets_needs_reconciliation() -> None:
    events = [
        _stored("AgentContextLoaded", {"agent_id": "a1", "model_version": "v2"}, 1),
        _stored("CreditAnalysisCompleted", {"application_id": "loan-1"}, 2),
        _stored("CreditAnalysisRequested", {"application_id": "loan-2"}, 3),
    ]
    ctx = await reconstruct_agent_context("agent-a1-s1", _mock_store(events))
    assert ctx.needs_reconciliation is True
    assert ctx.pending_work == ["CreditAnalysisRequested"]
    assert ctx.last_completed_action == "CreditAnalysisCompleted"
    assert ctx.session_health_status == "NEEDS_RECONCILIATION"
    assert ctx.last_event_position == 3


@pytest.mark.asyncio
async def test_only_context_loaded_sets_needs_reconciliation() -> None:
    events = [
        _stored("AgentContextLoaded", {"agent_id": "a1", "model_version": "v1"}, 1),
    ]
    ctx = await reconstruct_agent_context("agent-a1-s1", _mock_store(events))
    assert ctx.needs_reconciliation is True
    assert ctx.pending_work == ["AgentContextLoaded"]
    assert ctx.last_completed_action is None
    assert ctx.session_health_status == "NEEDS_RECONCILIATION"
    assert ctx.last_event_position == 1


# ---------------------------------------------------------------------------
# Verbatim tail and summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_events_are_last_three() -> None:
    events = [
        _stored("AgentContextLoaded", {"agent_id": "a1", "model_version": "v1"}, i)
        for i in range(1, 7)
    ]
    ctx = await reconstruct_agent_context("agent-a1-s1", _mock_store(events))
    assert len(ctx.recent_events) == 3
    assert ctx.recent_events[0]["stream_position"] == 4
    assert ctx.recent_events[-1]["stream_position"] == 6


@pytest.mark.asyncio
async def test_summary_covers_older_events() -> None:
    events = [
        _stored("AgentContextLoaded", {"agent_id": "a1"}, 1),
        _stored("CreditAnalysisCompleted", {"application_id": "loan-1"}, 2),
        _stored("FraudScreeningCompleted", {"application_id": "loan-1"}, 3),
        _stored("DecisionGenerated", {"application_id": "loan-1"}, 4),
        _stored("SessionTerminated", {}, 5),
    ]
    ctx = await reconstruct_agent_context("agent-a1-s1", _mock_store(events))
    # 5 events, tail=3 → 2 summarised
    assert len(ctx.summary) == 2
    assert "AgentContextLoaded" in ctx.summary[0]
    assert len(ctx.recent_events) == 3


@pytest.mark.asyncio
async def test_fewer_than_tail_events_all_verbatim() -> None:
    events = [
        _stored("AgentContextLoaded", {"agent_id": "a1"}, 1),
        _stored("CreditAnalysisCompleted", {}, 2),
    ]
    ctx = await reconstruct_agent_context("agent-a1-s1", _mock_store(events))
    assert len(ctx.recent_events) == 2
    assert ctx.summary == []
