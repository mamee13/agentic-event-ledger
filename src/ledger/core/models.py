from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BaseEvent(BaseModel):
    """Base class for all domain events."""

    event_type: str
    event_version: int = 1
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoredEvent(BaseEvent):
    """Event as stored in and retrieved from the database."""

    event_id: UUID
    stream_id: str
    stream_position: int
    global_position: int
    recorded_at: datetime


class StreamMetadata(BaseModel):
    """Metadata for an event stream."""

    model_config = ConfigDict(from_attributes=True)

    stream_id: str
    aggregate_type: str
    current_version: int = 0
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Typed event catalogue — domain-owned payload schemas
# ---------------------------------------------------------------------------


class ApplicationSubmitted(BaseEvent):
    """A new loan application has been submitted."""

    event_type: str = "ApplicationSubmitted"

    class Payload(BaseModel):
        application_id: str
        applicant_id: str
        requested_amount_usd: float
        loan_purpose: str
        submission_channel: str
        submitted_at: str

    payload: dict[str, Any]  # kept as dict for store compatibility; Payload used for validation


class CreditAnalysisRequested(BaseEvent):
    """Credit analysis has been requested for a loan application."""

    event_type: str = "CreditAnalysisRequested"

    class Payload(BaseModel):
        application_id: str

    payload: dict[str, Any]


class CreditAnalysisCompleted(BaseEvent):
    """An AI agent has completed credit analysis."""

    event_type: str = "CreditAnalysisCompleted"
    event_version: int = 2

    class Payload(BaseModel):
        application_id: str
        agent_id: str
        session_id: str
        model_version: str
        confidence_score: float
        risk_tier: str
        recommended_limit_usd: float
        analysis_duration_ms: int
        input_data_hash: str
        reasoning: str
        risk_score_raw: float
        regulatory_basis: str

    payload: dict[str, Any]


class ComplianceCheckRequested(BaseEvent):
    """Compliance check has been initiated for a loan application."""

    event_type: str = "ComplianceCheckRequested"

    class Payload(BaseModel):
        application_id: str

    payload: dict[str, Any]


class ComplianceRulePassed(BaseEvent):
    """A compliance rule has passed for a loan application."""

    event_type: str = "ComplianceRulePassed"

    class Payload(BaseModel):
        application_id: str
        rule_id: str
        rule_version: str

    payload: dict[str, Any]


class ComplianceRuleFailed(BaseEvent):
    """A compliance rule has failed for a loan application."""

    event_type: str = "ComplianceRuleFailed"

    class Payload(BaseModel):
        application_id: str
        rule_id: str
        rule_version: str
        failure_reason: str

    payload: dict[str, Any]


class FraudScreeningCompleted(BaseEvent):
    """Fraud screening has been completed for a loan application."""

    event_type: str = "FraudScreeningCompleted"

    class Payload(BaseModel):
        application_id: str
        agent_id: str
        session_id: str
        fraud_score: float
        screening_result: str  # PASS | FAIL | REVIEW
        risk_indicators: list[str] = []

    payload: dict[str, Any]


class DecisionGenerated(BaseEvent):
    """An AI orchestrator has generated a loan decision."""

    event_type: str = "DecisionGenerated"
    event_version: int = 2

    class Payload(BaseModel):
        application_id: str
        orchestrator_agent_id: str
        recommendation: str  # APPROVE | DECLINE | REFER
        confidence_score: float
        contributing_agent_sessions: list[str]
        decision_basis_summary: str
        model_versions: dict[str, str]

    payload: dict[str, Any]


class HumanReviewCompleted(BaseEvent):
    """A human reviewer has recorded a final decision."""

    event_type: str = "HumanReviewCompleted"

    class Payload(BaseModel):
        application_id: str
        reviewer_id: str
        override: bool
        final_decision: str  # APPROVE | DECLINE
        override_reason: str | None = None

    payload: dict[str, Any]


class HumanReviewOverride(BaseEvent):
    """A human reviewer explicitly overrides prior automated analysis."""

    event_type: str = "HumanReviewOverride"

    class Payload(BaseModel):
        application_id: str
        reviewer_id: str
        override_reason: str

    payload: dict[str, Any]


class ApplicationApproved(BaseEvent):
    """A loan application has been approved."""

    event_type: str = "ApplicationApproved"

    class Payload(BaseModel):
        application_id: str
        approved_amount_usd: float
        interest_rate: float
        conditions: list[str]
        approved_by: str
        effective_date: str

    payload: dict[str, Any]


class ApplicationDeclined(BaseEvent):
    """A loan application has been declined."""

    event_type: str = "ApplicationDeclined"

    class Payload(BaseModel):
        application_id: str
        adverse_action_notice_required: bool = False

    payload: dict[str, Any]


class AgentContextLoaded(BaseEvent):
    """An AI agent session has been initialised with its context."""

    event_type: str = "AgentContextLoaded"

    class Payload(BaseModel):
        agent_id: str
        session_id: str
        model_version: str
        context_source: str
        event_replay_from_position: int
        context_token_count: int

    payload: dict[str, Any]


class SessionTerminated(BaseEvent):
    """An AI agent session has been terminated."""

    event_type: str = "SessionTerminated"

    class Payload(BaseModel):
        session_id: str
        reason: str | None = None

    payload: dict[str, Any]


class AgentSessionClosed(BaseEvent):
    """An AI agent session has been closed explicitly."""

    event_type: str = "AgentSessionClosed"

    class Payload(BaseModel):
        agent_id: str
        session_id: str
        reason: str | None = None

    payload: dict[str, Any]


class DecisionOrchestratorSessionStarted(BaseEvent):
    """Decision Orchestrator session started (Gas Town pattern entry event)."""

    event_type: str = "DecisionOrchestratorSessionStarted"

    class Payload(BaseModel):
        agent_id: str
        session_id: str
        model_version: str
        context_source: str
        event_replay_from_position: int
        context_token_count: int

    payload: dict[str, Any]


class FraudScreeningRequested(BaseEvent):
    """Fraud screening has been requested for a loan application."""

    event_type: str = "FraudScreeningRequested"

    class Payload(BaseModel):
        application_id: str

    payload: dict[str, Any]


class ApplicationWithdrawn(BaseEvent):
    """A loan application has been withdrawn by the applicant."""

    event_type: str = "ApplicationWithdrawn"

    class Payload(BaseModel):
        application_id: str
        reason: str | None = None
        withdrawn_at: str

    payload: dict[str, Any]


class ComplianceClearanceIssued(BaseEvent):
    """A final compliance clearance has been issued for the entire application."""

    event_type: str = "ComplianceClearanceIssued"

    class Payload(BaseModel):
        application_id: str
        cleared_by: str
        regulation_set: str

    payload: dict[str, Any]


class AuditStreamInitialised(BaseEvent):
    """An audit stream has been initialised for an entity."""

    event_type: str = "AuditStreamInitialised"

    class Payload(BaseModel):
        entity_id: str
        entity_type: str
        initialised_at: str

    payload: dict[str, Any]
