"""What-If Projector — counterfactual scenario analysis.

Never writes to the real store. Loads real events, injects counterfactual
events at the branch point, replays causally independent real events, and
returns a comparison of real vs counterfactual outcomes.

Algorithm:
1. Load all events for the loan stream up to (but not including) the branch event type.
2. Inject the counterfactual event(s) instead of the real branch event.
3. Continue replaying real events that are causally independent of the branch.
4. Skip real events that are causally dependent on the branch.
5. Return real_outcome, counterfactual_outcome, divergence_events[].

Causal dependency rules:
- Events that reference fields changed by the counterfactual are dependent.
- For CreditAnalysisCompleted: risk_tier change → DecisionGenerated is dependent
  (recommendation may differ), ApplicationApproved/Declined are dependent.
- All other events (ApplicationSubmitted, AgentContextLoaded, compliance events)
  are causally independent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ledger.core.models import BaseEvent, StoredEvent

if TYPE_CHECKING:
    from ledger.infrastructure.store import EventStore

# Events that are causally dependent on a CreditAnalysisCompleted branch.
# If the risk_tier changes, these events' outcomes may differ.
_CREDIT_DEPENDENT_EVENTS = {
    "DecisionGenerated",
    "ApplicationApproved",
    "ApplicationDeclined",
    "HumanReviewCompleted",
}


@dataclass
class WhatIfResult:
    """Result of a what-if scenario analysis."""

    application_id: str
    branch_event_type: str
    real_outcome: dict[str, Any]
    counterfactual_outcome: dict[str, Any]
    divergence_events: list[str]  # event types that differ between real and counterfactual
    events_replayed: int
    events_skipped: int


def _apply_events_to_summary(events: Sequence[StoredEvent | BaseEvent]) -> dict[str, Any]:
    """Replay events into a minimal ApplicationSummary-like dict.

    This is a pure in-memory projection — no DB required.
    Mirrors the logic in ApplicationSummaryProjection.
    """
    state: dict[str, Any] = {
        "state": "SUBMITTED",
        "risk_tier": None,
        "fraud_score": None,
        "compliance_status": None,
        "decision": None,
        "confidence_score": None,
        "final_decision": None,
        "human_reviewer_id": None,
        "approved_amount_usd": None,
    }

    for event in events:
        e_type = event.event_type
        payload = event.payload

        if e_type == "ApplicationSubmitted":
            state["state"] = "SUBMITTED"

        elif e_type == "CreditAnalysisRequested":
            state["state"] = "AWAITING_ANALYSIS"

        elif e_type == "CreditAnalysisCompleted":
            state["state"] = "ANALYSIS_COMPLETE"
            state["risk_tier"] = payload.get("risk_tier")
            state["confidence_score"] = payload.get("confidence_score")

        elif e_type == "ComplianceCheckRequested":
            state["state"] = "COMPLIANCE_REVIEW"

        elif e_type == "ComplianceRulePassed":
            state["compliance_status"] = "PASSED"
            state["state"] = "PENDING_DECISION"

        elif e_type == "ComplianceRuleFailed":
            state["compliance_status"] = "FAILED"

        elif e_type == "FraudScreeningCompleted":
            state["fraud_score"] = payload.get("fraud_score")

        elif e_type == "DecisionGenerated":
            confidence = payload.get("confidence_score", 1.0)
            recommendation = payload.get("recommendation", "REFER")
            # Apply confidence floor
            if float(confidence) < 0.6:
                recommendation = "REFER"
            state["decision"] = recommendation
            state["confidence_score"] = confidence
            if recommendation == "APPROVE":
                state["state"] = "APPROVED_PENDING_HUMAN"
            elif recommendation == "DECLINE":
                state["state"] = "DECLINED_PENDING_HUMAN"
            else:
                state["state"] = "REFERRED"

        elif e_type == "HumanReviewCompleted":
            final = payload.get("final_decision")
            state["human_reviewer_id"] = payload.get("reviewer_id")
            state["final_decision"] = final
            if final == "APPROVE":
                state["state"] = "FINAL_APPROVED"
            elif final == "DECLINE":
                state["state"] = "FINAL_DECLINED"

        elif e_type == "ApplicationApproved":
            state["state"] = "FINAL_APPROVED"
            state["approved_amount_usd"] = payload.get("approved_amount_usd")

        elif e_type == "ApplicationDeclined":
            state["state"] = "FINAL_DECLINED"

    return state


def _is_causally_dependent(event_type: str, branch_event_type: str) -> bool:
    """Returns True if event_type is causally dependent on the branch event type."""
    if branch_event_type == "CreditAnalysisCompleted":
        return event_type in _CREDIT_DEPENDENT_EVENTS
    # Default: no dependency known — treat as independent
    return False


async def run_whatif(
    application_id: str,
    store: EventStore,
    branch_event_type: str,
    counterfactual_payload: dict[str, Any],
    counterfactual_event_version: int = 2,
) -> WhatIfResult:
    """Run a what-if scenario for a loan application.

    Args:
        application_id: The loan application ID (without 'loan-' prefix).
        store: The event store to load real events from.
        branch_event_type: The event type at which to branch (e.g. 'CreditAnalysisCompleted').
        counterfactual_payload: The payload to use for the counterfactual branch event.
        counterfactual_event_version: Event version for the counterfactual event.

    Returns:
        WhatIfResult with real and counterfactual outcomes.
    """
    stream_id = f"loan-{application_id}"
    real_events = await store.load_stream(stream_id)

    # Split events: before branch, the branch itself, and after branch
    pre_branch: list[StoredEvent] = []
    branch_event: StoredEvent | None = None
    post_branch: list[StoredEvent] = []

    for event in real_events:
        if branch_event is None and event.event_type == branch_event_type:
            branch_event = event
        elif branch_event is None:
            pre_branch.append(event)
        else:
            post_branch.append(event)

    # Build counterfactual event — same metadata as real branch, different payload
    cf_event = BaseEvent(
        event_type=branch_event_type,
        event_version=counterfactual_event_version,
        payload=counterfactual_payload,
    )

    # Real replay: all events as-is
    real_replay: list[StoredEvent | BaseEvent] = list(real_events)

    # Counterfactual replay: pre-branch + cf_event + causally independent post-branch events
    cf_replay: list[StoredEvent | BaseEvent] = list(pre_branch) + [cf_event]
    skipped: list[str] = []
    replayed_count = len(pre_branch) + 1  # pre-branch + cf event

    for event in post_branch:
        if _is_causally_dependent(event.event_type, branch_event_type):
            skipped.append(event.event_type)
        else:
            cf_replay.append(event)
            replayed_count += 1

    # Compute outcomes
    real_outcome = _apply_events_to_summary(real_replay)
    cf_outcome = _apply_events_to_summary(cf_replay)

    # Find divergence: fields that differ between real and counterfactual
    divergence_events: list[str] = []
    for key in real_outcome:
        if real_outcome[key] != cf_outcome.get(key):
            divergence_events.append(key)

    return WhatIfResult(
        application_id=application_id,
        branch_event_type=branch_event_type,
        real_outcome=real_outcome,
        counterfactual_outcome=cf_outcome,
        divergence_events=divergence_events,
        events_replayed=replayed_count,
        events_skipped=len(skipped),
    )
