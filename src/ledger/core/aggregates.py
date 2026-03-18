from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar

from ledger.core.errors import DomainRuleError
from ledger.core.models import BaseEvent, StoredEvent

if TYPE_CHECKING:
    from ledger.infrastructure.store import EventStore

T = TypeVar("T")
Self = TypeVar("Self", bound="BaseAggregate[object]")

# Re-export DomainRuleError as DomainError so existing tests keep working
DomainError = DomainRuleError


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
            self.version += 1

    @abstractmethod
    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        """Handles state transitions for specific events."""
        pass

    @classmethod
    async def load(cls, id: str, store: "EventStore") -> "BaseAggregate[T]":
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

    @classmethod
    async def load(cls, id: str, store: "EventStore") -> "LoanApplicationAggregate":
        events = await store.load_stream(id)
        agg = cls(id)
        agg.load_from_history(events)
        return agg

    def __init__(self, loan_id: str):
        super().__init__(loan_id)
        self.state: LoanState = LoanState.SUBMITTED
        self.model_versions: set[str] = set()
        self.has_human_override: bool = False
        self.is_compliance_passed: bool = False
        self.contributing_sessions: list[str] = []

    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:  # noqa: C901
        e_type = event.event_type

        if e_type == "ApplicationSubmitted":
            # Explicit handler: sets initial state on replay
            self.state = LoanState.SUBMITTED

        elif e_type == "CreditAnalysisRequested":
            if self.state != LoanState.SUBMITTED:
                raise DomainRuleError(
                    rule_name="state_machine",
                    message=f"CreditAnalysisRequested invalid from state {self.state}",
                    suggested_action="Only request analysis from SUBMITTED state.",
                )
            self.state = LoanState.AWAITING_ANALYSIS

        elif e_type == "CreditAnalysisCompleted":
            # Rule 3: model-version locking — reject second analysis unless human override
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
            mod_ver = event.payload.get("model_version")
            if mod_ver:
                self.model_versions.add(str(mod_ver))
            self.state = LoanState.ANALYSIS_COMPLETE

        elif e_type == "ComplianceCheckRequested":
            if self.state != LoanState.ANALYSIS_COMPLETE:
                raise DomainRuleError(
                    rule_name="state_machine",
                    message=f"ComplianceCheckRequested invalid from state {self.state}",
                )
            self.state = LoanState.COMPLIANCE_REVIEW

        elif e_type == "ComplianceRulePassed":
            if self.state != LoanState.COMPLIANCE_REVIEW:
                raise DomainRuleError(
                    rule_name="state_machine",
                    message=f"ComplianceRulePassed invalid from state {self.state}",
                )
            self.is_compliance_passed = True
            self.state = LoanState.PENDING_DECISION

        elif e_type == "ComplianceRuleFailed":
            if self.state != LoanState.COMPLIANCE_REVIEW:
                raise DomainRuleError(
                    rule_name="state_machine",
                    message=f"ComplianceRuleFailed invalid from state {self.state}",
                )
            self.is_compliance_passed = False
            # Stay in COMPLIANCE_REVIEW — must be resolved before proceeding

        elif e_type == "DecisionGenerated":
            # Rule 4: must be in PENDING_DECISION
            if self.state != LoanState.PENDING_DECISION:
                raise DomainRuleError(
                    rule_name="state_machine",
                    message=(
                        f"DecisionGenerated only allowed from PENDING_DECISION "
                        f"(current: {self.state})"
                    ),
                )
            contribs = event.payload.get("contributing_agent_sessions", [])
            if not contribs:
                raise DomainRuleError(
                    rule_name="causal_chain",
                    message="DecisionGenerated must have contributing_agent_sessions",
                )
            self.contributing_sessions = list(contribs)

            # Rule 2: confidence floor — score < 0.6 forces REFER
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

        elif e_type == "HumanReviewCompleted":
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
            self.has_human_override = bool(event.payload.get("override", False))
            final_decision = event.payload.get("final_decision")
            if final_decision == "APPROVE":
                self.state = LoanState.FINAL_APPROVED
            elif final_decision == "DECLINE":
                self.state = LoanState.FINAL_DECLINED
            # If final_decision is absent/other, state stays (e.g. REFERRED awaiting re-decision)

        elif e_type == "ApplicationApproved":
            # Rule 5: compliance dependency — must be cleared before approval
            if not self.is_compliance_passed:
                raise DomainRuleError(
                    rule_name="compliance_dependency",
                    message=(
                        f"Rule 5 Violation: ApplicationApproved blocked for {self.id}; "
                        "ComplianceRecord stream is not PASSED"
                    ),
                    suggested_action="Ensure all ComplianceRulePassed events are recorded first.",
                )
            self.state = LoanState.FINAL_APPROVED

        elif e_type == "ApplicationDeclined":
            self.state = LoanState.FINAL_DECLINED

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
    """Aggregate for an AI agent's session.

    Gas Town rule: AgentContextLoaded MUST be the first event.
    No analysis or decision event is allowed before it.
    """

    @classmethod
    async def load(cls, id: str, store: "EventStore") -> "AgentSessionAggregate":
        events = await store.load_stream(id)
        agg = cls(id)
        agg.load_from_history(events)
        return agg

    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.context_loaded: bool = False
        self.is_active: bool = False
        self.agent_id: str | None = None
        self.model_version: str | None = None
        self.contributed_apps: set[str] = set()

    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        e_type = event.event_type

        # Rule 1 (Gas Town): enforce context-first loading
        if e_type != "AgentContextLoaded" and not self.context_loaded:
            raise DomainRuleError(
                rule_name="gas_town",
                message=f"Cannot apply {e_type} before AgentContextLoaded",
                suggested_action="Start the session with AgentContextLoaded first.",
            )

        if e_type == "AgentContextLoaded":
            # Must be the very first event (version == -1 means no events yet)
            if self.version != -1:
                raise DomainRuleError(
                    rule_name="gas_town",
                    message="AgentContextLoaded must be the first event in the session stream",
                    suggested_action="Do not replay AgentContextLoaded after session has started.",
                )
            self.context_loaded = True
            self.is_active = True
            self.agent_id = str(event.payload.get("agent_id", "unknown"))
            self.model_version = str(event.payload.get("model_version", "unknown"))

        elif e_type == "CreditAnalysisCompleted":
            app_id = event.payload.get("application_id")
            if app_id:
                self.contributed_apps.add(str(app_id))

        elif e_type == "FraudScreeningCompleted":
            # v1 handler — track contributed application
            app_id = event.payload.get("application_id")
            if app_id:
                self.contributed_apps.add(str(app_id))

        elif e_type == "DecisionGenerated":
            app_id = event.payload.get("application_id")
            if app_id:
                self.contributed_apps.add(str(app_id))

        elif e_type == "SessionTerminated":
            self.is_active = False


class ComplianceRecordAggregate(BaseAggregate[str]):
    """Aggregate for a loan's compliance record.

    Lives on stream: compliance-{application_id}
    Tracks per-rule results and exposes is_passed for the LoanApplication approval gate.
    """

    def __init__(self, application_id: str):
        super().__init__(application_id)
        self.application_id = application_id
        self.results: dict[str, str] = {}
        self.is_passed: bool = False
        self.check_requested: bool = False

    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        e_type = event.event_type

        if e_type == "ComplianceCheckRequested":
            self.check_requested = True

        elif e_type == "ComplianceRulePassed":
            rule_id = str(event.payload.get("rule_id", "default"))
            self.results[rule_id] = "PASSED"

        elif e_type == "ComplianceRuleFailed":
            rule_id = str(event.payload.get("rule_id", "default"))
            self.results[rule_id] = "FAILED"

        # is_passed = all recorded rules passed AND at least one rule exists
        self.is_passed = bool(self.results) and all(v == "PASSED" for v in self.results.values())


class AuditLedgerAggregate(BaseAggregate[str]):
    """Aggregate for the cryptographic audit chain.

    Lives on stream: audit-{entity_type}-{entity_id}
    Tracks integrity hash chain for tamper detection (Phase 5).
    """

    def __init__(self, stream_id: str):
        super().__init__(stream_id)
        self.last_integrity_hash: str | None = None
        self.check_count: int = 0

    def _apply_to_state(self, event: BaseEvent | StoredEvent) -> None:
        if event.event_type == "AuditIntegrityCheckRun":
            self.last_integrity_hash = str(event.payload.get("integrity_hash", ""))
            self.check_count += 1
