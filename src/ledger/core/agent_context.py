"""reconstruct_agent_context — Gas Town crash recovery.

Loads the full AgentSession stream and produces a structured context summary
that is sufficient for an agent to resume work after a crash.

Rules:
- Keep the last 3 events verbatim.
- Summarise all earlier events as one-line strings.
- Flag NEEDS_RECONCILIATION if the last event is a "partial" event
  (i.e. a *Requested or *Started event with no matching *Completed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ledger.infrastructure.store import EventStore

# Events that represent incomplete / in-flight work when they are the last event
_PARTIAL_EVENT_TYPES = {
    "CreditAnalysisRequested",
    "FraudScreeningRequested",
    "ComplianceCheckRequested",
    "AgentContextLoaded",  # session started but nothing done yet
}


@dataclass
class AgentContextSummary:
    session_id: str
    agent_id: str | None
    model_version: str | None
    is_active: bool
    last_completed_action: str | None
    pending_work: str | None
    needs_reconciliation: bool
    summary: list[str]  # one line per older event
    recent_events: list[dict[str, Any]]  # last 3 verbatim payloads
    total_events: int


def _summarise(event_type: str, payload: dict[str, Any]) -> str:
    """One-line human-readable summary of an event."""
    app = payload.get("application_id", "")
    suffix = f" for {app}" if app else ""
    return f"{event_type}{suffix}"


async def reconstruct_agent_context(
    session_id: str,
    store: EventStore,
    verbatim_tail: int = 3,
) -> AgentContextSummary:
    """Loads the AgentSession stream and builds a resumable context summary."""
    events = await store.load_stream(session_id)

    if not events:
        return AgentContextSummary(
            session_id=session_id,
            agent_id=None,
            model_version=None,
            is_active=False,
            last_completed_action=None,
            pending_work=None,
            needs_reconciliation=False,
            summary=[],
            recent_events=[],
            total_events=0,
        )

    # Extract agent metadata from the first event (AgentContextLoaded)
    first = events[0]
    agent_id: str | None = first.payload.get("agent_id")
    model_version: str | None = first.payload.get("model_version")

    # Determine active status
    is_active = events[-1].event_type != "SessionTerminated"

    # Identify last completed action and pending work
    last_completed: str | None = None
    pending: str | None = None
    last_event_type = events[-1].event_type

    if last_event_type in _PARTIAL_EVENT_TYPES:
        pending = last_event_type
        # Walk backwards to find the last completed action
        for e in reversed(events[:-1]):
            if e.event_type not in _PARTIAL_EVENT_TYPES:
                last_completed = e.event_type
                break
    else:
        last_completed = last_event_type

    # NEEDS_RECONCILIATION: last event is partial/unfinished
    needs_reconciliation = last_event_type in _PARTIAL_EVENT_TYPES

    # Split into summary (older) and verbatim tail (recent)
    tail_events = events[-verbatim_tail:]
    older_events = events[:-verbatim_tail] if len(events) > verbatim_tail else []

    summary = [_summarise(e.event_type, e.payload) for e in older_events]
    recent = [
        {
            "event_type": e.event_type,
            "stream_position": e.stream_position,
            "recorded_at": e.recorded_at.isoformat(),
            "payload": e.payload,
        }
        for e in tail_events
    ]

    return AgentContextSummary(
        session_id=session_id,
        agent_id=agent_id,
        model_version=model_version,
        is_active=is_active,
        last_completed_action=last_completed,
        pending_work=pending,
        needs_reconciliation=needs_reconciliation,
        summary=summary,
        recent_events=recent,
        total_events=len(events),
    )
