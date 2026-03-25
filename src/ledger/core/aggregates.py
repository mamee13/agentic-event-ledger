from abc import ABC
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, Self, TypeVar

from ledger.core.errors import DomainRuleError
from ledger.core.models import BaseEvent, StoredEvent

if TYPE_CHECKING:
    from ledger.infrastructure.store import EventStore

T = TypeVar("T")


class BaseAggregate(ABC, Generic[T]):
    """Base class for all domain aggregates."""

    def __init__(self, id: str):
        self.id = id
        self.version = -1
        self.changes: list[BaseEvent] = []

    def apply(self, event: BaseEvent | StoredEvent, is_new: bool = True) -> None:
        """Applies an event to the aggregate and updates state.

        Guards are only enforced for new events (is_new=True). Replay from
        history skips guards so that stored events are always accepted.
        """
        if is_new:
            self._guard_event(event)
        self._apply_to_state(event)
        if isinstance(event, StoredEvent):
            self.version = event.stream_position
        elif is_new:
            self.changes.append(event)
            self.version += 1

    def _guard_event(self, event: BaseEvent | StoredEvent) -> None:
        """Dispatches to a guard method before applying state. Override in subclasses."""
        method_name = f"_guard_{self._to_snake_case(event.event_type)}"
        guard = getattr(self, method_name, None)
        if guard:
            guard(event)

    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        """Dispatches the event to a specific handler method."""
        method_name = f"_apply_{self._to_snake_case(event.event_type)}"
        handler = getattr(self, method_name, None)
        if handler:
            handler(event)

    def _to_snake_case(self, name: str) -> str:
        """Converts PascalCase event type to snake_case."""
        import re

        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

    @classmethod
    async def load(cls, id: str, store: "EventStore") -> Self:
        """Replays the stream to rehydrate the aggregate and sets its version."""
        events = await store.load_stream(id)
        aggregate = cls(id)
        aggregate.load_from_history(events)
        return aggregate

    def load_from_history(self, events: list[StoredEvent]) -> None:
        """Rehydrates the aggregate from a list of stored events."""
        for event in events:
            self.apply(event, is_new=False)


class LoanState(StrEnum):
    SUBMITTED = "SUBMITTED"
    AWAITING_ANALYSIS = "AWAITING_ANALYSIS"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    COMPLIANCE_REVIEW = "COMPLIANCE_REVIEW"
    PENDING_DECISION = "PENDING_DECISION"
    APPROVED_PENDING_HUMAN = "APPROVED_PENDING_HUMAN"
    DECLINED_PENDING_HUMAN = "DECLINED_PENDING_HUMAN"
    REFERRED = "REFERRED"
    FINAL_APPROVED = "FINAL_APPROVED"
    FINAL_DECLINED = "FINAL_DECLINED"


class LoanApplicationAggregate(BaseAggregate[LoanState]):
    """Aggregate for a loan application lifecycle.

    State machine:
      SUBMITTED → AWAITING_ANALYSIS → ANALYSIS_COMPLETE → COMPLIANCE_REVIEW
      → PENDING_DECISION → APPROVED_PENDING_HUMAN | DECLINED_PENDING_HUMAN | REFERRED
      → FINAL_APPROVED | FINAL_DECLINED
    """

    def __init__(self, loan_id: str):
        super().__init__(loan_id)
        self.state: LoanState = LoanState.SUBMITTED
        self.model_versions: set[str] = set()
        self.has_human_override: bool = False
        self.is_compliance_passed: bool = False
        self.contributing_sessions: list[str] = []

    # ------------------------------------------------------------------ dispatch

    def _apply_application_submitted(self, _event: BaseEvent | StoredEvent) -> None:
        self.state = LoanState.SUBMITTED

    def _apply_credit_analysis_requested(self, _event: BaseEvent | StoredEvent) -> None:
        self.state = LoanState.AWAITING_ANALYSIS

    def _apply_credit_analysis_completed(self, event: BaseEvent | StoredEvent) -> None:
        mod_ver = event.payload.get("model_version")
        if mod_ver:
            self.model_versions.add(str(mod_ver))
        self.state = LoanState.ANALYSIS_COMPLETE

    def _apply_compliance_check_requested(self, _event: BaseEvent | StoredEvent) -> None:
        self.state = LoanState.COMPLIANCE_REVIEW

    def _apply_compliance_rule_passed(self, _event: BaseEvent | StoredEvent) -> None:
        self.is_compliance_passed = True
        self.state = LoanState.PENDING_DECISION

    def _apply_compliance_rule_failed(self, _event: BaseEvent | StoredEvent) -> None:
        self.is_compliance_passed = False

    def _apply_decision_generated(self, event: BaseEvent | StoredEvent) -> None:
        self.contributing_sessions = list(event.payload.get("contributing_agent_sessions", []))
        confidence = event.payload.get("confidence_score")
        if confidence is not None and float(confidence) < 0.6:
            self.state = LoanState.REFERRED
        else:
            rec = event.payload.get("recommendation")
            if rec == "APPROVE":
                self.state = LoanState.APPROVED_PENDING_HUMAN
            elif rec == "DECLINE":
                self.state = LoanState.DECLINED_PENDING_HUMAN
            else:
                self.state = LoanState.REFERRED

    def _apply_human_review_completed(self, event: BaseEvent | StoredEvent) -> None:
        self.has_human_override = bool(event.payload.get("override", False))
        final_decision = event.payload.get("final_decision")
        if final_decision == "APPROVE":
            self.state = LoanState.FINAL_APPROVED
        elif final_decision == "DECLINE":
            self.state = LoanState.FINAL_DECLINED

    def _apply_application_approved(self, _event: BaseEvent | StoredEvent) -> None:
        self.state = LoanState.FINAL_APPROVED

    def _apply_application_declined(self, _event: BaseEvent | StoredEvent) -> None:
        self.state = LoanState.FINAL_DECLINED

    # ------------------------------------------------------------------ guards

    def _guard_credit_analysis_requested(self, _event: BaseEvent | StoredEvent) -> None:
        self.guard_request_credit_analysis()

    def _guard_credit_analysis_completed(self, _event: BaseEvent | StoredEvent) -> None:
        self.guard_record_credit_analysis()

    def _guard_compliance_check_requested(self, _event: BaseEvent | StoredEvent) -> None:
        self.guard_request_compliance_check()

    def _guard_compliance_rule_passed(self, _event: BaseEvent | StoredEvent) -> None:
        self.guard_record_compliance()

    def _guard_compliance_rule_failed(self, _event: BaseEvent | StoredEvent) -> None:
        self.guard_record_compliance()

    def _guard_decision_generated(self, event: BaseEvent | StoredEvent) -> None:
        self.guard_generate_decision(event.payload)

    def _guard_human_review_completed(self, event: BaseEvent | StoredEvent) -> None:
        override = bool(event.payload.get("override", False))
        override_reason = event.payload.get("override_reason")
        self.guard_human_review(override, override_reason)

    def _guard_application_approved(self, _event: BaseEvent | StoredEvent) -> None:
        self.guard_finalize_approval(self.is_compliance_passed)

    def guard_request_credit_analysis(self) -> None:
        if self.state != LoanState.SUBMITTED:
            raise DomainRuleError(
                rule_name="state_machine",
                message=f"CreditAnalysisRequested invalid from state {self.state}",
                suggested_action="Only request analysis from SUBMITTED state.",
            )

    def guard_record_credit_analysis(self) -> None:
        if self.model_versions and not self.has_human_override:
            raise DomainRuleError(
                rule_name="model_version_locking",
                message=(
                    f"Rule 3 Violation: Rejecting further analysis for application {self.id} "
                    "without human override."
                ),
                suggested_action="A HumanReviewCompleted with override=True is required.",
            )
        if self.state not in (LoanState.AWAITING_ANALYSIS, LoanState.ANALYSIS_COMPLETE):
            raise DomainRuleError(
                rule_name="state_machine",
                message=f"CreditAnalysisCompleted invalid from state {self.state}",
            )

    def guard_request_compliance_check(self) -> None:
        if self.state != LoanState.ANALYSIS_COMPLETE:
            raise DomainRuleError(
                rule_name="state_machine",
                message=f"ComplianceCheckRequested invalid from state {self.state}",
            )

    def guard_record_compliance(self) -> None:
        if self.state != LoanState.COMPLIANCE_REVIEW:
            raise DomainRuleError(
                rule_name="state_machine",
                message=f"Compliance event invalid from state {self.state}",
            )

    def guard_generate_decision(self, event_payload: dict[str, Any]) -> None:
        if self.state != LoanState.PENDING_DECISION:
            raise DomainRuleError(
                rule_name="state_machine",
                message=(
                    f"DecisionGenerated only allowed from PENDING_DECISION (current: {self.state})"
                ),
            )
        if not event_payload.get("contributing_agent_sessions"):
            raise DomainRuleError(
                rule_name="causal_chain",
                message="DecisionGenerated must have contributing_agent_sessions",
            )

    def guard_human_review(self, _override: bool, _override_reason: str | None) -> None:
        valid_states = {
            LoanState.APPROVED_PENDING_HUMAN,
            LoanState.DECLINED_PENDING_HUMAN,
            LoanState.REFERRED,
        }
        if self.state not in valid_states:
            raise DomainRuleError(
                rule_name="state_machine",
                message=f"HumanReviewCompleted invalid from state {self.state}",
            )

    def guard_finalize_approval(self, is_compliance_passed: bool) -> None:
        if not is_compliance_passed:
            raise DomainRuleError(
                rule_name="compliance_dependency",
                message=(
                    f"Rule 5 Violation: ApplicationApproved blocked for {self.id}; "
                    "ComplianceRecord stream is not PASSED"
                ),
                suggested_action="Ensure all ComplianceRulePassed events are recorded first.",
            )

    def validate_causal_chain(self, sessions: list["AgentSessionAggregate"]) -> None:
        """Rule 6: verify every contributing session actually processed this loan.

        The loan_id stored in session.contributed_apps may be bare (e.g. '123') or
        prefixed ('loan-123'). We normalise both sides before comparing.
        """
        bare_id = self.id.removeprefix("loan-")
        prefixed_id = f"loan-{bare_id}"
        for session in sessions:
            contributed = {a.removeprefix("loan-") for a in session.contributed_apps}
            if bare_id not in contributed and prefixed_id not in session.contributed_apps:
                raise DomainRuleError(
                    rule_name="causal_chain",
                    message=(
                        f"Causal Chain Violation: Session {session.id} "
                        f"did not process application {self.id}"
                    ),
                    suggested_action="Only include sessions that recorded analysis for this loan.",
                )


class AgentSessionAggregate(BaseAggregate[str]):
    """Aggregate for an AI agent's session."""

    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.context_loaded: bool = False
        self.is_active: bool = False
        self.agent_id: str | None = None
        self.model_version: str | None = None
        self.contributed_apps: set[str] = set()

    # ------------------------------------------------------------------ dispatch

    def _apply_agent_context_loaded(self, event: BaseEvent | StoredEvent) -> None:
        self.context_loaded = True
        self.is_active = True
        self.agent_id = str(event.payload.get("agent_id", "unknown"))
        self.model_version = str(event.payload.get("model_version", "unknown"))

    def _apply_credit_analysis_completed(self, event: BaseEvent | StoredEvent) -> None:
        app_id = event.payload.get("application_id")
        if app_id:
            self.contributed_apps.add(str(app_id))

    def _apply_fraud_screening_completed(self, event: BaseEvent | StoredEvent) -> None:
        app_id = event.payload.get("application_id")
        if app_id:
            self.contributed_apps.add(str(app_id))

    def _apply_decision_generated(self, event: BaseEvent | StoredEvent) -> None:
        app_id = event.payload.get("application_id")
        if app_id:
            self.contributed_apps.add(str(app_id))

    def _apply_session_terminated(self, _event: BaseEvent | StoredEvent) -> None:
        self.is_active = False

    # ------------------------------------------------------------------ guards

    def _guard_agent_context_loaded(self, _event: BaseEvent | StoredEvent) -> None:
        if self.context_loaded:
            raise DomainRuleError(
                rule_name="gas_town",
                message=(
                    f"AgentContextLoaded must be the first event in session {self.id}; "
                    "context is already loaded."
                ),
                suggested_action="Do not replay AgentContextLoaded after session has started.",
            )

    def _guard_credit_analysis_completed(self, _event: BaseEvent | StoredEvent) -> None:
        if not self.context_loaded:
            raise DomainRuleError(
                rule_name="gas_town",
                message=(
                    f"Session {self.id}: CreditAnalysisCompleted recorded before AgentContextLoaded"
                ),
                suggested_action="Ensure AgentContextLoaded is the first event on this session.",
            )

    def _guard_fraud_screening_completed(self, _event: BaseEvent | StoredEvent) -> None:
        if not self.context_loaded:
            raise DomainRuleError(
                rule_name="gas_town",
                message=(
                    f"Session {self.id}: FraudScreeningCompleted recorded before AgentContextLoaded"
                ),
                suggested_action="Ensure AgentContextLoaded is the first event on this session.",
            )

    def _guard_decision_generated(self, _event: BaseEvent | StoredEvent) -> None:
        if not self.context_loaded:
            raise DomainRuleError(
                rule_name="gas_town",
                message=(
                    f"Session {self.id}: DecisionGenerated recorded before AgentContextLoaded"
                ),
                suggested_action="Ensure AgentContextLoaded is the first event on this session.",
            )

    def guard_start_session(self) -> None:
        if self.version != -1:
            raise DomainRuleError(
                rule_name="gas_town",
                message="AgentContextLoaded must be the first event in the session stream",
                suggested_action="Do not replay AgentContextLoaded after session has started.",
            )

    def guard_context_loaded(self) -> None:
        """Guard: raises if AgentContextLoaded has not yet been replayed."""
        if not self.context_loaded:
            raise DomainRuleError(
                rule_name="gas_town",
                message=f"Session {self.id} has no loaded context — AgentContextLoaded required",
                suggested_action="Ensure AgentContextLoaded is the first event on this session.",
            )

    def guard_model_version(self, declared_model_version: str) -> None:
        """Raises if declared model version differs from one locked at context load."""
        self.guard_context_loaded()
        if self.model_version != declared_model_version:
            raise DomainRuleError(
                rule_name="model_version_locking",
                message=(
                    f"Model version mismatch on session {self.id}: "
                    f"context locked to '{self.model_version}', "
                    f"command declared '{declared_model_version}'"
                ),
                suggested_action="Start a new session with the correct model version.",
            )


class ComplianceRecordAggregate(BaseAggregate[str]):
    """Aggregate for a loan's compliance record."""

    def __init__(self, application_id: str):
        super().__init__(application_id)
        self.application_id = application_id
        self.results: dict[str, str] = {}
        self.required_rules: set[str] = set()
        self.is_passed: bool = False
        self.check_requested: bool = False

    def _apply_compliance_check_requested(self, event: BaseEvent | StoredEvent) -> None:
        self.check_requested = True
        rules = event.payload.get("required_rules")
        if rules:
            self.required_rules = set(rules)

    def _apply_compliance_rule_passed(self, _event: BaseEvent | StoredEvent) -> None:
        rule_id = str(_event.payload.get("rule_id", "default"))
        self.results[rule_id] = "PASSED"
        self._update_is_passed()

    def _apply_compliance_rule_failed(self, _event: BaseEvent | StoredEvent) -> None:
        rule_id = str(_event.payload.get("rule_id", "default"))
        self.results[rule_id] = "FAILED"
        self._update_is_passed()

    def _update_is_passed(self) -> None:
        if not self.required_rules:
            # Fallback if no rules were specified:
            # at least one rule must have passed, and none failed
            self.is_passed = bool(self.results) and all(
                v == "PASSED" for v in self.results.values()
            )
        else:
            # All required rules must be present and PASSED
            self.is_passed = all(self.results.get(rule) == "PASSED" for rule in self.required_rules)


class AuditLedgerAggregate(BaseAggregate[str]):
    """Aggregate for the cryptographic audit chain.

    Lives on stream: audit-{entity_type}-{entity_id}
    Tracks integrity hash chain for tamper detection (Phase 5).
    """

    def __init__(self, stream_id: str):
        super().__init__(stream_id)
        self.last_integrity_hash: str | None = None
        self.check_count: int = 0

    def _apply_audit_integrity_check_run(self, event: BaseEvent | StoredEvent) -> None:
        self.last_integrity_hash = str(event.payload.get("integrity_hash", ""))
        self.check_count += 1
