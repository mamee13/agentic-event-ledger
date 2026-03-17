from ledger.core.aggregates import AgentSessionAggregate, LoanApplicationAggregate, LoanState
from ledger.core.models import BaseEvent


def test_loan_aggregate_transitions() -> None:
    loan = LoanApplicationAggregate("loan-123")
    assert loan.state == LoanState.SUBMITTED

    # 1. Start evaluation
    loan.apply(BaseEvent(event_type="EvaluationStarted", payload={}))
    assert loan.state == LoanState.UNDER_REVIEW

    # 2. Decision Approve (with confidence)
    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated", payload={"decision": "APPROVE", "confidence_score": 0.8}
        )
    )
    assert loan.state == LoanState.APPROVED


def test_loan_aggregate_confidence_floor() -> None:
    loan = LoanApplicationAggregate("loan-123")
    loan.apply(BaseEvent(event_type="EvaluationStarted", payload={}))

    # Decision Approve but LOW confidence -> REFERRED
    loan.apply(
        BaseEvent(
            event_type="DecisionGenerated", payload={"decision": "APPROVE", "confidence_score": 0.5}
        )
    )
    assert loan.state == LoanState.REFERRED


def test_loan_aggregate_compliance() -> None:
    loan = LoanApplicationAggregate("loan-123")
    assert not loan.compliance_passed

    # One pass is not enough if there are multiple rules (default logic)
    loan.apply(BaseEvent(event_type="ComplianceCheckPassed", payload={"rule_id": "R1"}))
    assert loan.compliance_passed

    # If one fails, it's not passed
    loan.apply(BaseEvent(event_type="ComplianceCheckFailed", payload={"rule_id": "R2"}))
    assert not loan.compliance_passed


def test_loan_aggregate_model_locking() -> None:
    loan = LoanApplicationAggregate("loan-123")
    loan.apply(
        BaseEvent(
            event_type="CreditAnalysisCompleted",
            payload={"risk_score": 0.5, "model_version": "GPT-4"},
        )
    )
    assert "GPT-4" in loan.model_versions
    assert loan.analysis_count == 1


def test_agent_session_lifecycle() -> None:
    session = AgentSessionAggregate("sess-1")
    assert not session.is_active

    session.apply(
        BaseEvent(
            event_type="AgentContextLoaded", payload={"agent_id": "bot-1", "model_version": "v1"}
        )
    )
    assert session.is_active
    assert session.context_loaded

    session.apply(BaseEvent(event_type="SessionTerminated", payload={}))
    assert not session.is_active
