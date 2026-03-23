"""Unit tests for all domain aggregates.

Covers:
  - Full state machine happy path
  - Every business rule with positive and negative cases
  - Invalid state transitions
  - Gas Town (Rule 1)
  - Confidence floor (Rule 2)
  - Model-version locking (Rule 3)
  - Compliance dependency (Rule 5)
  - Causal chain (Rule 6)
  - ComplianceRecordAggregate per-rule tracking
  - AuditLedgerAggregate hash tracking
"""

from uuid import uuid4

import pytest

from ledger.core.aggregates import (
    AgentSessionAggregate,
    AuditLedgerAggregate,
    ComplianceRecordAggregate,
    LoanApplicationAggregate,
    LoanState,
)
from ledger.core.errors import DomainError, DomainRuleError
from ledger.core.models import BaseEvent

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_loan(loan_id: str = "loan-123") -> LoanApplicationAggregate:
    return LoanApplicationAggregate(loan_id)


def _make_session(session_id: str = "sess-1") -> AgentSessionAggregate:
    return AgentSessionAggregate(session_id)


def _s(loan: LoanApplicationAggregate) -> str:
    """Cast state to plain str so mypy doesn't flag Literal comparison-overlap."""
    return str(loan.state)


def _advance_to_pending_decision(loan: LoanApplicationAggregate) -> None:
    """Drive a loan aggregate to PENDING_DECISION via the happy path."""
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))
    loan.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))
    loan.apply(BaseEvent(event_type="ComplianceRulePassed", payload={"rule_id": "R1"}))


# ---------------------------------------------------------------------------
# LoanState enum
# ---------------------------------------------------------------------------


def test_loan_state_is_str_enum() -> None:
    assert LoanState.SUBMITTED == "SUBMITTED"
    assert isinstance(LoanState.SUBMITTED, str)


# ---------------------------------------------------------------------------
# Happy-path state machine
# ---------------------------------------------------------------------------


def test_full_happy_path_approve() -> None:
    loan = _make_loan()
    s = _s(loan)
    assert s == LoanState.SUBMITTED

    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    s = _s(loan)
    assert s == LoanState.AWAITING_ANALYSIS

    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))

    loan.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))
    s = _s(loan)
    assert s == LoanState.COMPLIANCE_REVIEW

    loan.apply(BaseEvent(event_type="ComplianceRulePassed", payload={"rule_id": "R1"}))
    s = _s(loan)
    assert s == LoanState.PENDING_DECISION
    assert loan.is_compliance_passed

    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "recommendation": "APPROVE",
                "confidence_score": 0.9,
                "contributing_agent_sessions": ["sess-1"],
            },
        )
    )
    s = _s(loan)
    assert s == LoanState.APPROVED_PENDING_HUMAN

    loan.apply(
        BaseEvent(
            event_type="HumanReviewCompleted",
            payload={"override": False, "final_decision": "APPROVE"},
        )
    )
    s = _s(loan)
    assert s == LoanState.FINAL_APPROVED


def test_full_happy_path_decline() -> None:
    loan = _make_loan()
    _advance_to_pending_decision(loan)

    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "recommendation": "DECLINE",
                "confidence_score": 0.85,
                "contributing_agent_sessions": ["sess-1"],
            },
        )
    )
    s = _s(loan)
    assert s == LoanState.DECLINED_PENDING_HUMAN

    loan.apply(
        BaseEvent(
            event_type="HumanReviewCompleted",
            payload={"override": False, "final_decision": "DECLINE"},
        )
    )
    s = _s(loan)
    assert s == LoanState.FINAL_DECLINED


def test_application_submitted_explicit_handler_on_replay() -> None:
    """ApplicationSubmitted must explicitly set SUBMITTED on replay."""
    loan = _make_loan()
    # Simulate replay: apply as a stored event
    from datetime import UTC, datetime

    from ledger.core.models import StoredEvent

    stored = StoredEvent(
        event_id=uuid4(),
        event_type="ApplicationSubmitted",
        payload={},
        stream_id="loan-123",
        stream_position=1,
        global_position=1,
        recorded_at=datetime.now(UTC),
    )
    loan.apply(stored, is_new=False)
    assert loan.state == LoanState.SUBMITTED
    assert loan.version == 1


# ---------------------------------------------------------------------------
# Rule 1 — Gas Town
# ---------------------------------------------------------------------------


def test_gas_town_blocks_analysis_before_context_loaded() -> None:
    session = _make_session()
    with pytest.raises(DomainError, match="before AgentContextLoaded"):
        session.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={}))


def test_gas_town_blocks_fraud_screening_before_context_loaded() -> None:
    session = _make_session()
    with pytest.raises(DomainError, match="before AgentContextLoaded"):
        session.apply(BaseEvent(event_type="FraudScreeningCompleted", payload={}))


def test_gas_town_blocks_decision_before_context_loaded() -> None:
    session = _make_session()
    with pytest.raises(DomainError, match="before AgentContextLoaded"):
        session.apply(BaseEvent(event_type="DecisionGenerated", payload={}))


def test_gas_town_context_loaded_must_be_first() -> None:
    """AgentContextLoaded cannot be applied a second time."""
    session = _make_session()
    session.apply(BaseEvent(event_type="AgentContextLoaded", payload={"model_version": "v1"}))
    with pytest.raises(DomainError, match="first event"):
        session.apply(BaseEvent(event_type="AgentContextLoaded", payload={"model_version": "v2"}))


def test_gas_town_happy_path() -> None:
    session = _make_session()
    session.apply(
        BaseEvent(
            event_type="AgentContextLoaded",
            payload={"agent_id": "agent-1", "model_version": "v2"},
        )
    )
    assert session.context_loaded
    assert session.is_active
    assert session.model_version == "v2"
    assert session.agent_id == "agent-1"


# ---------------------------------------------------------------------------
# Rule 2 — Confidence floor
# ---------------------------------------------------------------------------


def test_confidence_floor_below_threshold_forces_refer() -> None:
    loan = _make_loan()
    _advance_to_pending_decision(loan)

    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "recommendation": "APPROVE",
                "confidence_score": 0.55,
                "contributing_agent_sessions": ["sess-1"],
            },
        )
    )
    assert loan.state == LoanState.REFERRED


def test_confidence_floor_exactly_at_threshold_passes() -> None:
    loan = _make_loan()
    _advance_to_pending_decision(loan)

    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "recommendation": "APPROVE",
                "confidence_score": 0.6,
                "contributing_agent_sessions": ["sess-1"],
            },
        )
    )
    assert loan.state == LoanState.APPROVED_PENDING_HUMAN


def test_confidence_floor_missing_score_uses_recommendation() -> None:
    loan = _make_loan()
    _advance_to_pending_decision(loan)

    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "recommendation": "DECLINE",
                "contributing_agent_sessions": ["sess-1"],
            },
        )
    )
    assert loan.state == LoanState.DECLINED_PENDING_HUMAN


# ---------------------------------------------------------------------------
# Rule 3 — Model-version locking
# ---------------------------------------------------------------------------


def test_model_version_locking_rejects_second_analysis() -> None:
    loan = _make_loan()
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))
    assert "v1" in loan.model_versions

    with pytest.raises(DomainError, match="Rule 3 Violation"):
        loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))


def test_model_version_locking_allows_second_after_human_override() -> None:
    """Rule 3: after HumanReviewCompleted with override=True, has_human_override is set.
    A second CreditAnalysisCompleted is then allowed IF the state machine is back in
    AWAITING_ANALYSIS (i.e. the loan was re-opened for re-analysis).
    We test the flag is set and the lock is lifted by driving the state back manually.
    """
    loan = _make_loan()
    _advance_to_pending_decision(loan)

    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "recommendation": "APPROVE",
                "confidence_score": 0.8,
                "contributing_agent_sessions": ["sess-1"],
            },
        )
    )
    loan.apply(
        BaseEvent(
            event_type="HumanReviewCompleted",
            payload={"override": True, "final_decision": "APPROVE"},
        )
    )
    assert loan.has_human_override

    # The override flag is set — Rule 3 lock is lifted.
    # Manually reset state to AWAITING_ANALYSIS to simulate a re-analysis workflow.
    loan.state = LoanState.AWAITING_ANALYSIS
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v2"}))
    assert "v2" in loan.model_versions


# ---------------------------------------------------------------------------
# Rule 5 — Compliance dependency
# ---------------------------------------------------------------------------


def test_compliance_dependency_blocks_approval_without_clearance() -> None:
    loan = _make_loan()
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))
    loan.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))

    assert not loan.is_compliance_passed

    with pytest.raises(DomainError, match="ComplianceRecord stream is not PASSED"):
        loan.apply(BaseEvent(event_type="ApplicationApproved", payload={}))


def test_compliance_dependency_allows_approval_after_clearance() -> None:
    loan = _make_loan()
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))
    loan.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))
    loan.apply(BaseEvent(event_type="ComplianceRulePassed", payload={"rule_id": "R1"}))

    assert loan.is_compliance_passed

    loan.apply(BaseEvent(event_type="ApplicationApproved", payload={}))
    assert loan.state == LoanState.FINAL_APPROVED


def test_compliance_rule_failed_keeps_review_state() -> None:
    loan = _make_loan()
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))
    loan.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))
    loan.apply(BaseEvent(event_type="ComplianceRuleFailed", payload={"rule_id": "R1"}))

    assert loan.state == LoanState.COMPLIANCE_REVIEW
    assert not loan.is_compliance_passed


# ---------------------------------------------------------------------------
# Rule 6 — Causal chain
# ---------------------------------------------------------------------------


def test_causal_chain_rejects_session_that_did_not_process_loan() -> None:
    loan = _make_loan("loan-123")
    session = _make_session("sess-1")
    session.apply(BaseEvent(event_type="AgentContextLoaded", payload={"model_version": "v1"}))

    # Session has not contributed to loan-123
    with pytest.raises(DomainError, match="did not process application"):
        loan.validate_causal_chain([session])


def test_causal_chain_accepts_session_that_processed_loan() -> None:
    loan = _make_loan("loan-123")
    session = _make_session("sess-1")
    session.apply(BaseEvent(event_type="AgentContextLoaded", payload={"model_version": "v1"}))
    session.apply(
        BaseEvent(event_type="CreditAnalysisCompleted", payload={"application_id": "loan-123"})
    )

    # Should not raise
    loan.validate_causal_chain([session])


def test_causal_chain_normalises_bare_vs_prefixed_id() -> None:
    """Session stores bare '123', loan id is 'loan-123' — both should match."""
    loan = _make_loan("loan-123")
    session = _make_session("sess-1")
    session.apply(BaseEvent(event_type="AgentContextLoaded", payload={}))
    # Store bare id (no 'loan-' prefix)
    session.apply(
        BaseEvent(event_type="CreditAnalysisCompleted", payload={"application_id": "123"})
    )

    loan.validate_causal_chain([session])


def test_decision_generated_requires_contributing_sessions() -> None:
    loan = _make_loan()
    _advance_to_pending_decision(loan)

    with pytest.raises(DomainError, match="contributing_agent_sessions"):
        loan.apply(
            BaseEvent(
                event_type="DecisionGenerated",
                payload={
                    "recommendation": "APPROVE",
                    "confidence_score": 0.9,
                    "contributing_agent_sessions": [],
                },
            )
        )


# ---------------------------------------------------------------------------
# Invalid state transitions
# ---------------------------------------------------------------------------


def test_invalid_transition_decision_from_analysis_complete() -> None:
    """DecisionGenerated is not allowed from ANALYSIS_COMPLETE — must go through compliance."""
    loan = _make_loan()
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))
    assert loan.state == LoanState.ANALYSIS_COMPLETE

    with pytest.raises(DomainError, match="PENDING_DECISION"):
        loan.apply(
            BaseEvent(
                event_type="DecisionGenerated",
                payload={
                    "recommendation": "APPROVE",
                    "confidence_score": 0.9,
                    "contributing_agent_sessions": ["sess-1"],
                },
            )
        )


def test_invalid_transition_analysis_from_compliance_review() -> None:
    loan = _make_loan()
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v1"}))
    loan.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))

    with pytest.raises(DomainError, match="Rule 3 Violation"):
        loan.apply(BaseEvent(event_type="CreditAnalysisCompleted", payload={"model_version": "v2"}))


def test_invalid_transition_compliance_from_submitted() -> None:
    loan = _make_loan()
    with pytest.raises(DomainError, match="invalid from state"):
        loan.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))


def test_invalid_transition_human_review_from_submitted() -> None:
    loan = _make_loan()
    with pytest.raises(DomainError, match="invalid from state"):
        loan.apply(
            BaseEvent(
                event_type="HumanReviewCompleted",
                payload={"override": False, "final_decision": "APPROVE"},
            )
        )


def test_invalid_transition_compliance_rule_from_submitted() -> None:
    loan = _make_loan()
    with pytest.raises(DomainError, match="invalid from state"):
        loan.apply(BaseEvent(event_type="ComplianceRulePassed", payload={"rule_id": "R1"}))


def test_invalid_transition_compliance_rule_failed_from_submitted() -> None:
    loan = _make_loan()
    with pytest.raises(DomainError, match="invalid from state"):
        loan.apply(BaseEvent(event_type="ComplianceRuleFailed", payload={"rule_id": "R1"}))


def test_invalid_transition_analysis_requested_from_awaiting() -> None:
    loan = _make_loan()
    loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))
    with pytest.raises(DomainError, match="invalid from state"):
        loan.apply(BaseEvent(event_type="CreditAnalysisRequested", payload={}))


# ---------------------------------------------------------------------------
# ComplianceRecordAggregate
# ---------------------------------------------------------------------------


def test_compliance_record_single_rule_passed() -> None:
    comp = ComplianceRecordAggregate("loan-123")
    comp.apply(BaseEvent(event_type="ComplianceRulePassed", payload={"rule_id": "R1"}))
    assert comp.is_passed
    assert comp.results["R1"] == "PASSED"


def test_compliance_record_one_fail_makes_not_passed() -> None:
    comp = ComplianceRecordAggregate("loan-123")
    comp.apply(BaseEvent(event_type="ComplianceRulePassed", payload={"rule_id": "R1"}))
    comp.apply(BaseEvent(event_type="ComplianceRuleFailed", payload={"rule_id": "R2"}))
    assert not comp.is_passed
    assert comp.results["R2"] == "FAILED"


def test_compliance_record_empty_is_not_passed() -> None:
    comp = ComplianceRecordAggregate("loan-123")
    assert not comp.is_passed


def test_compliance_record_check_requested_sets_flag() -> None:
    comp = ComplianceRecordAggregate("loan-123")
    assert not comp.check_requested
    comp.apply(BaseEvent(event_type="ComplianceCheckRequested", payload={}))
    assert comp.check_requested


def test_compliance_record_multiple_rules_all_passed() -> None:
    comp = ComplianceRecordAggregate("loan-123")
    for rule in ("AML", "KYC", "FRAUD"):
        comp.apply(BaseEvent(event_type="ComplianceRulePassed", payload={"rule_id": rule}))
    assert comp.is_passed
    assert len(comp.results) == 3


# ---------------------------------------------------------------------------
# AgentSessionAggregate — FraudScreeningCompleted tracking
# ---------------------------------------------------------------------------


def test_agent_session_tracks_fraud_screening_app() -> None:
    session = _make_session()
    session.apply(BaseEvent(event_type="AgentContextLoaded", payload={"model_version": "v1"}))
    session.apply(
        BaseEvent(
            event_type="FraudScreeningCompleted",
            payload={"application_id": "loan-456"},
        )
    )
    assert "loan-456" in session.contributed_apps


def test_agent_session_terminated_sets_inactive() -> None:
    session = _make_session()
    session.apply(BaseEvent(event_type="AgentContextLoaded", payload={}))
    assert session.is_active
    session.apply(BaseEvent(event_type="SessionTerminated", payload={}))
    assert not session.is_active


def test_agent_session_tracks_multiple_apps() -> None:
    session = _make_session()
    session.apply(BaseEvent(event_type="AgentContextLoaded", payload={}))
    session.apply(
        BaseEvent(event_type="CreditAnalysisCompleted", payload={"application_id": "loan-1"})
    )
    session.apply(
        BaseEvent(event_type="FraudScreeningCompleted", payload={"application_id": "loan-2"})
    )
    assert "loan-1" in session.contributed_apps
    assert "loan-2" in session.contributed_apps


# ---------------------------------------------------------------------------
# AuditLedgerAggregate
# ---------------------------------------------------------------------------


def test_audit_ledger_tracks_integrity_hash() -> None:
    audit = AuditLedgerAggregate("audit-loan-123")
    assert audit.last_integrity_hash is None
    assert audit.check_count == 0

    audit.apply(
        BaseEvent(
            event_type="AuditIntegrityCheckRun",
            payload={"integrity_hash": "abc123", "previous_hash": ""},
        )
    )
    assert audit.last_integrity_hash == "abc123"
    assert audit.check_count == 1


def test_audit_ledger_increments_check_count() -> None:
    audit = AuditLedgerAggregate("audit-loan-123")
    for i in range(3):
        audit.apply(
            BaseEvent(
                event_type="AuditIntegrityCheckRun",
                payload={"integrity_hash": f"hash-{i}", "previous_hash": ""},
            )
        )
    assert audit.check_count == 3
    assert audit.last_integrity_hash == "hash-2"


# ---------------------------------------------------------------------------
# DomainError / DomainRuleError compatibility
# ---------------------------------------------------------------------------


def test_domain_rule_error_has_structured_dict() -> None:
    err = DomainRuleError(
        rule_name="test_rule",
        message="something went wrong",
        suggested_action="fix it",
    )
    data = err.to_dict()
    assert data["error_code"] == "INVALID_STATE_TRANSITION"
    assert data["details"]["rule_name"] == "test_rule"
    assert data["suggested_action"] == "fix it"
