"""Regulatory Package Generator.

Produces a self-contained JSON artifact for a loan application that contains:
  1. Full ordered event stream with payloads (as of examination_date).
  2. Projection states as of examination_date.
  3. Integrity verification result and hash chain summary.
  4. Human-readable narrative (one sentence per significant event).
  5. Model versions, confidence scores, and input hashes for all contributing agents.

The package is independently verifiable — it contains everything a regulator
needs to reconstruct and audit the decision without access to the live system.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ledger.core.audit_chain import verify_chain

if TYPE_CHECKING:
    from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
    from ledger.infrastructure.store import EventStore


# ---------------------------------------------------------------------------
# Narrative generation
# ---------------------------------------------------------------------------


def _narrative(event_type: str, payload: dict[str, Any]) -> str:
    """One sentence per significant event."""
    app = payload.get("application_id", "")
    match event_type:
        case "ApplicationSubmitted":
            amt = payload.get("requested_amount_usd", "?")
            applicant = payload.get("applicant_id", "?")
            return f"Application {app} submitted by {applicant} for ${amt}."
        case "CreditAnalysisRequested":
            return f"Credit analysis requested for application {app}."
        case "CreditAnalysisCompleted":
            tier = payload.get("risk_tier", "?")
            model = payload.get("model_version", "?")
            conf = payload.get("confidence_score")
            conf_str = f" with confidence {conf:.2f}" if conf is not None else ""
            return f"Credit analysis completed: risk tier {tier}{conf_str} (model {model})."
        case "ComplianceCheckRequested":
            return f"Compliance check initiated for application {app}."
        case "ComplianceRulePassed":
            rule = payload.get("rule_id", "?")
            return f"Compliance rule '{rule}' passed for application {app}."
        case "ComplianceRuleFailed":
            rule = payload.get("rule_id", "?")
            reason = payload.get("failure_reason", "")
            return f"Compliance rule '{rule}' failed for application {app}: {reason}."
        case "FraudScreeningCompleted":
            score = payload.get("fraud_score", "?")
            result = payload.get("screening_result", "?")
            return f"Fraud screening completed: score {score}, result {result}."
        case "DecisionGenerated":
            rec = payload.get("recommendation", "?")
            conf = payload.get("confidence_score", "?")
            return f"Decision generated: {rec} (confidence {conf})."
        case "HumanReviewCompleted":
            reviewer = payload.get("reviewer_id", "?")
            decision = payload.get("final_decision", "?")
            override = " (override)" if payload.get("override") else ""
            return f"Human reviewer {reviewer} recorded final decision: {decision}{override}."
        case "ApplicationApproved":
            amt = payload.get("approved_amount_usd", "?")
            return f"Application {app} approved for ${amt}."
        case "ApplicationDeclined":
            return f"Application {app} declined."
        case "AuditIntegrityCheckRun":
            n = payload.get("events_hashed", "?")
            return f"Integrity check run: {n} events hashed."
        case _:
            return f"Event {event_type} recorded for application {app}."


# ---------------------------------------------------------------------------
# Agent attribution extraction
# ---------------------------------------------------------------------------


def _extract_agent_attribution(events: list[Any]) -> list[dict[str, Any]]:
    """Extract model versions, confidence scores, and input hashes from agent events."""
    attributions: list[dict[str, Any]] = []
    seen: set[str] = set()

    for event in events:
        payload = event.payload if hasattr(event, "payload") else event.get("payload", {})
        e_type = event.event_type if hasattr(event, "event_type") else event.get("event_type", "")

        if e_type == "CreditAnalysisCompleted":
            agent_id = payload.get("agent_id")
            session_id = payload.get("session_id")
            key = f"{agent_id}-{session_id}-credit"
            if key not in seen:
                seen.add(key)
                attributions.append(
                    {
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "event_type": e_type,
                        "model_version": payload.get("model_version"),
                        "confidence_score": payload.get("confidence_score"),
                        "input_data_hash": payload.get("input_data_hash"),
                        "regulatory_basis": payload.get("regulatory_basis"),
                        "risk_tier": payload.get("risk_tier"),
                    }
                )

        elif e_type == "DecisionGenerated":
            agent_id = payload.get("orchestrator_agent_id")
            model_versions = payload.get("model_versions", {})
            key = f"{agent_id}-decision"
            if key not in seen:
                seen.add(key)
                attributions.append(
                    {
                        "agent_id": agent_id,
                        "session_id": None,
                        "event_type": e_type,
                        "model_version": model_versions.get(agent_id) if agent_id else None,
                        "model_versions_all": model_versions,
                        "confidence_score": payload.get("confidence_score"),
                        "input_data_hash": None,
                        "recommendation": payload.get("recommendation"),
                        "contributing_sessions": payload.get("contributing_agent_sessions", []),
                    }
                )

    return attributions


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


async def generate_regulatory_package(
    application_id: str,
    examination_date: datetime,
    store: EventStore,
    compliance_projection: ComplianceAuditViewProjection,
) -> dict[str, Any]:
    """Generate a self-contained regulatory package for a loan application.

    Args:
        application_id: The loan application ID (without 'loan-' prefix).
        examination_date: The point-in-time for projection states.
        store: The event store.
        compliance_projection: The compliance audit view projection.

    Returns:
        A dict that can be serialized to JSON as the regulatory package.
    """
    loan_stream_id = f"loan-{application_id}"
    audit_stream_id = f"audit-loan-{application_id}"

    # 1. Load full ordered event stream as of examination_date
    all_events = await store.load_stream(loan_stream_id)
    events_as_of = [e for e in all_events if e.recorded_at <= examination_date]

    serialized_events = [
        {
            "event_id": str(e.event_id),
            "event_type": e.event_type,
            "event_version": e.event_version,
            "stream_position": e.stream_position,
            "global_position": e.global_position,
            "recorded_at": e.recorded_at.isoformat(),
            "payload": e.payload,
        }
        for e in events_as_of
    ]

    # 2. Projection states as of examination_date
    compliance_state = await compliance_projection.get_compliance_at(
        application_id, examination_date
    )

    # Build application summary in-memory as of examination_date
    # (avoids needing a live projection table query with time filter)
    from ledger.core.whatif import _apply_events_to_summary

    app_summary = _apply_events_to_summary(events_as_of)

    projection_states = {
        "application_summary": app_summary,
        "compliance_audit_view": compliance_state,
    }

    # 3. Integrity verification — load audit stream raw for hash verification
    audit_events_raw = await store.load_stream_raw(audit_stream_id)
    if audit_events_raw:
        check_runs = [e for e in audit_events_raw if e.event_type == "AuditIntegrityCheckRun"]
        is_valid, integrity_message = verify_chain(check_runs, audit_events_raw)
        last_hash = check_runs[-1].payload.get("integrity_hash", "") if check_runs else ""
        hash_chain_summary = {
            "total_check_runs": len(check_runs),
            "last_integrity_hash": last_hash,
            "chain_valid": is_valid,
            "verification_message": integrity_message,
        }
    else:
        is_valid = True
        integrity_message = "No audit stream found — integrity checks not yet run."
        hash_chain_summary = {
            "total_check_runs": 0,
            "last_integrity_hash": "",
            "chain_valid": True,
            "verification_message": integrity_message,
        }

    integrity_verification = {
        "is_valid": is_valid,
        "message": integrity_message,
        "hash_chain_summary": hash_chain_summary,
    }

    # 4. Human-readable narrative
    narrative = [
        {
            "recorded_at": e.recorded_at.isoformat(),
            "event_type": e.event_type,
            "sentence": _narrative(e.event_type, e.payload),
        }
        for e in events_as_of
        if e.event_type not in {"AuditIntegrityCheckRun"}
    ]

    # 5. Agent attribution
    agent_attribution = _extract_agent_attribution(events_as_of)

    # Package checksum — SHA-256 of the canonical event stream
    canonical = json.dumps(serialized_events, sort_keys=True, separators=(",", ":"))
    package_checksum = hashlib.sha256(canonical.encode()).hexdigest()

    return {
        "schema_version": "1.0",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "application_id": application_id,
        "examination_date": examination_date.isoformat(),
        "package_checksum": package_checksum,
        "event_stream": serialized_events,
        "projection_states": projection_states,
        "integrity_verification": integrity_verification,
        "narrative": narrative,
        "agent_attribution": agent_attribution,
    }
