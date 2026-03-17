from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from ledger.core.models import BaseEvent, StoredEvent

T = TypeVar("T")


class BaseAggregate(ABC, Generic[T]):
    """Base class for all domain aggregates."""

    def __init__(self, id: str):
        self.id = id
        self.version = -1
        self.changes: list[BaseEvent] = []

    def apply(self, event: BaseEvent | StoredEvent, is_new: bool = True) -> None:
        """Applies an event to the aggregate and updates state."""
        self._apply_to_state(event)
        if isinstance(event, StoredEvent):
            self.version = event.stream_position
        elif is_new:
            self.changes.append(event)

    @abstractmethod
    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        """Handles state transitions for specific events."""
        pass

    def load_from_history(self, events: list[StoredEvent]) -> None:
        """Rehydrates the aggregate from a list of stored events."""
        for event in events:
            self.apply(event, is_new=False)


class LoanState:
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    REFERRED = "REFERRED"


class LoanApplicationAggregate(BaseAggregate[LoanState]):
    """Aggregate for a loan application lifecycle."""

    def __init__(self, loan_id: str):
        super().__init__(loan_id)
        self.state = LoanState.SUBMITTED
        self.compliance_passed = False
        self.analysis_count = 0
        self.model_versions: set[str] = set()
        self.compliance_results: dict[str, str] = {}

    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        e_type = event.event_type

        if e_type == "ApplicationSubmitted":
            self.state = LoanState.SUBMITTED
        elif e_type == "EvaluationStarted":
            if self.state == LoanState.SUBMITTED:
                self.state = LoanState.UNDER_REVIEW
        elif e_type == "ComplianceCheckPassed":
            self.compliance_results[event.payload.get("rule_id", "default")] = "PASSED"
            self.compliance_passed = all(
                res == "PASSED" for res in self.compliance_results.values()
            )
        elif e_type == "ComplianceCheckFailed":
            self.compliance_results[event.payload.get("rule_id", "default")] = "FAILED"
            self.compliance_passed = False
        elif e_type == "CreditAnalysisCompleted":
            self.analysis_count += 1
            mod_ver = event.payload.get("model_version")
            if mod_ver:
                self.model_versions.add(mod_ver)
        elif e_type == "DecisionGenerated":
            # Rule: confidence < 0.6 -> REFERRED
            confidence = event.payload.get("confidence_score")
            # We use float() cast because it might be coming from JSONB as string or float
            if confidence is not None and float(confidence) < 0.6:
                self.state = LoanState.REFERRED
            else:
                decision = event.payload.get("decision")
                if decision == "APPROVE":
                    self.state = LoanState.APPROVED
                elif decision == "DECLINE":
                    self.state = LoanState.DECLINED
                else:
                    self.state = LoanState.REFERRED


class AgentSessionAggregate(BaseAggregate[str]):
    """Aggregate for an AI agent's session."""

    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.context_loaded = False
        self.is_active = False
        self.agent_id: str | None = None

    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        e_type = event.event_type

        if e_type == "AgentContextLoaded":
            # Enforcement: this must be the first event (stream position 1)
            if self.version != -1:
                # In a real system, we'd raise a DomainError here, but apply()
                # should be safe. Validation happens in the Service Layer.
                pass
            self.context_loaded = True
            self.is_active = True
            self.agent_id = event.payload.get("agent_id", "unknown")
        elif e_type == "SessionTerminated":
            self.is_active = False
