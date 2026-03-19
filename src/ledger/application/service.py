"""Command handlers for The Ledger.

Pattern: load aggregates -> validate rules -> build event -> append with expected_version.

Stream ID formats:
  loan-{application_id}
  agent-{agent_id}-{session_id}
  compliance-{application_id}
  audit-{entity_type}-{entity_id}
"""

from ledger.core.aggregates import (
    AgentSessionAggregate,
    ComplianceRecordAggregate,
    LoanApplicationAggregate,
)
from ledger.core.audit_chain import run_integrity_check
from ledger.core.errors import DomainRuleError
from ledger.core.models import BaseEvent
from ledger.infrastructure.store import EventStore


class LedgerService:
    def __init__(self, store: EventStore):
        self.store = store

    # ------------------------------------------------------------------ loaders

    async def _load_loan(self, loan_id: str) -> LoanApplicationAggregate:
        return await LoanApplicationAggregate.load(f"loan-{loan_id}", self.store)

    async def _load_session(self, agent_id: str, session_id: str) -> AgentSessionAggregate:
        return await AgentSessionAggregate.load(f"agent-{agent_id}-{session_id}", self.store)

    async def _load_compliance(self, loan_id: str) -> ComplianceRecordAggregate:
        """Load compliance aggregate; returns empty aggregate if stream does not exist yet."""
        stream_id = f"compliance-{loan_id}"
        events = await self.store.load_stream(stream_id)
        compliance = ComplianceRecordAggregate(loan_id)
        compliance.load_from_history(events)
        return compliance

    # ------------------------------------------------------------------ commands

    async def submit_application(self, loan_id: str, amount: float, applicant_id: str) -> None:
        """Submit a new loan application (creates the loan stream)."""
        event = BaseEvent(
            event_type="ApplicationSubmitted",
            payload={
                "application_id": loan_id,
                "applicant_id": applicant_id,
                "requested_amount_usd": amount,
                "loan_purpose": "Business loan",
                "submission_channel": "Web",
                "submitted_at": "2026-03-17T00:00:00Z",
            },
        )
        await self.store.append(f"loan-{loan_id}", [event], expected_version=-1)

    async def start_agent_session(self, session_id: str, agent_id: str, model_version: str) -> None:
        """Start a new agent session -- writes AgentContextLoaded as the first event."""
        event = BaseEvent(
            event_type="AgentContextLoaded",
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "context_source": "MLflow Registry",
                "event_replay_from_position": 0,
                "context_token_count": 1024,
                "model_version": model_version,
            },
        )
        await self.store.append(f"agent-{agent_id}-{session_id}", [event], expected_version=-1)

    async def request_credit_analysis(self, loan_id: str) -> None:
        """Transition loan to AWAITING_ANALYSIS."""
        loan = await self._load_loan(loan_id)
        event = BaseEvent(event_type="CreditAnalysisRequested", payload={"application_id": loan_id})
        await self.store.append(f"loan-{loan_id}", [event], expected_version=loan.version)

    async def record_credit_analysis(
        self,
        loan_id: str,
        agent_id: str,
        session_id: str,
        risk_score: float,
        reasoning: str,
        analysis_duration_ms: int = 0,
    ) -> None:
        """Record a completed credit analysis on the loan stream AND the agent session stream.

        Rule 3 (model-version locking) is enforced by the loan aggregate.
        The event is appended to both streams so:
          - The loan state machine advances to ANALYSIS_COMPLETE.
          - The AgentSession stream records the contribution, enabling causal chain
            validation and DecisionGenerated v1→v2 upcasting.
        """
        session = await self._load_session(agent_id, session_id)
        loan = await self._load_loan(loan_id)

        event = BaseEvent(
            event_type="CreditAnalysisCompleted",
            event_version=2,
            payload={
                "application_id": loan_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "model_version": session.model_version,
                "confidence_score": 0.85,
                "risk_tier": "MEDIUM",
                "recommended_limit_usd": 50000.0,
                "analysis_duration_ms": analysis_duration_ms,
                "input_data_hash": "sha256-abc123",
                "reasoning": reasoning,
                "risk_score_raw": risk_score,
                "regulatory_basis": "EU-AI-ACT-2025",
            },
        )

        # Capture versions before apply() mutates them
        loan_version = loan.version
        session_version = session.version

        # Validate rules in-memory before writing
        loan.apply(event)
        session.apply(event)

        # Append to loan stream
        await self.store.append(f"loan-{loan_id}", [event], expected_version=loan_version)

        # Append to agent session stream so contributed_apps is persisted
        session_stream = f"agent-{agent_id}-{session_id}"
        await self.store.append(session_stream, [event], expected_version=session_version)

    async def request_compliance_check(self, loan_id: str) -> None:
        """Transition loan to COMPLIANCE_REVIEW and open the compliance stream.

        ComplianceCheckRequested goes to BOTH streams:
          - loan stream: drives the loan state machine to COMPLIANCE_REVIEW
          - compliance stream: opens the compliance record for rule results
        """
        loan = await self._load_loan(loan_id)
        compliance = await self._load_compliance(loan_id)

        loan_event = BaseEvent(
            event_type="ComplianceCheckRequested", payload={"application_id": loan_id}
        )
        comp_event = BaseEvent(
            event_type="ComplianceCheckRequested", payload={"application_id": loan_id}
        )

        await self.store.append(f"loan-{loan_id}", [loan_event], expected_version=loan.version)
        comp_expected = compliance.version if compliance.version >= 0 else -1
        await self.store.append(
            f"compliance-{loan_id}", [comp_event], expected_version=comp_expected
        )

    async def record_compliance(self, loan_id: str, rule_id: str, status: str) -> None:
        """Record a compliance rule result.

        ComplianceRulePassed / ComplianceRuleFailed go to the compliance stream.
        When all rules pass, a ComplianceRulePassed event is also appended to the
        loan stream so the loan state machine advances from COMPLIANCE_REVIEW to
        PENDING_DECISION.
        """
        compliance = await self._load_compliance(loan_id)

        event_type = "ComplianceRulePassed" if status == "PASSED" else "ComplianceRuleFailed"
        payload: dict[str, str] = {
            "application_id": loan_id,
            "rule_id": rule_id,
            "rule_version": "1.0",
        }
        if status != "PASSED":
            payload["failure_reason"] = "Compliance requirement not met"

        event = BaseEvent(event_type=event_type, payload=payload)
        comp_expected = compliance.version if compliance.version >= 0 else -1
        await self.store.append(f"compliance-{loan_id}", [event], expected_version=comp_expected)

        # Apply the result in-memory to check if all rules now pass
        was_passed = compliance.is_passed
        compliance.apply(event)

        # Emit clearance to loan stream only on False → True transition
        if not was_passed and compliance.is_passed:
            loan = await self._load_loan(loan_id)
            loan_clearance = BaseEvent(
                event_type="ComplianceRulePassed",
                payload={
                    "application_id": loan_id,
                    "rule_id": "compliance_clearance",
                    "rule_version": "1.0",
                },
            )
            # Capture version before apply() increments it, then validate
            loan_version = loan.version
            loan.apply(loan_clearance)
            await self.store.append(
                f"loan-{loan_id}", [loan_clearance], expected_version=loan_version
            )

    async def generate_decision(
        self,
        loan_id: str,
        agent_id: str,
        session_id: str,
        recommendation: str,
        confidence: float,
        contributing_sessions: list[dict[str, str]],
    ) -> None:
        """Generate a decision for a loan application.

        Rule 6 (causal chain): all contributing sessions must have processed this loan.
        Rule 2 (confidence floor): confidence < 0.6 forces REFER.
        Rule 4 (state machine): only valid from PENDING_DECISION.

        The event is appended to both the loan stream and the agent session stream so
        contributed_apps is persisted and Decision upcasting has accurate session data.
        """
        session = await self._load_session(agent_id, session_id)
        loan = await self._load_loan(loan_id)

        # Rule 6: load and validate all contributing sessions
        linked: list[AgentSessionAggregate] = []
        for contrib in contributing_sessions:
            c_stream = f"agent-{contrib['agent_id']}-{contrib['session_id']}"
            c_session = await AgentSessionAggregate.load(c_stream, self.store)
            linked.append(c_session)

        loan.validate_causal_chain(linked)

        # Build model_versions from all contributing sessions + orchestrator
        model_versions: dict[str, str] = {
            c_sess.agent_id: c_sess.model_version
            for c_sess in linked
            if c_sess.agent_id and c_sess.model_version
        }
        model_versions[agent_id] = session.model_version or "unknown"

        event = BaseEvent(
            event_type="DecisionGenerated",
            event_version=2,
            payload={
                "application_id": loan_id,
                "orchestrator_agent_id": agent_id,
                "recommendation": recommendation,
                "confidence_score": confidence,
                "contributing_agent_sessions": [
                    f"agent-{c['agent_id']}-{c['session_id']}" for c in contributing_sessions
                ],
                "decision_basis_summary": "All agents agree",
                "model_versions": model_versions,
            },
        )

        # Capture versions before apply() mutates them
        loan_version = loan.version
        session_version = session.version

        loan.apply(event)
        session.apply(event)

        # Append to loan stream
        await self.store.append(f"loan-{loan_id}", [event], expected_version=loan_version)

        # Append to agent session stream so contributed_apps is persisted
        session_stream = f"agent-{agent_id}-{session_id}"
        await self.store.append(session_stream, [event], expected_version=session_version)

    async def record_human_review(
        self,
        loan_id: str,
        reviewer_id: str,
        decision: str,
        override: bool = False,
        override_reason: str | None = None,
    ) -> None:
        """Record a human reviewer's decision.

        If override=True, override_reason is required.
        """
        if override and not override_reason:
            raise DomainRuleError(
                rule_name="human_review",
                message="override_reason is required when override=True",
                suggested_action="Provide a reason for the override.",
            )

        loan = await self._load_loan(loan_id)
        event = BaseEvent(
            event_type="HumanReviewCompleted",
            payload={
                "application_id": loan_id,
                "reviewer_id": reviewer_id,
                "override": override,
                "final_decision": decision,
                "override_reason": override_reason,
            },
        )
        await self.store.append(f"loan-{loan_id}", [event], expected_version=loan.version)

    async def finalize_approval(self, loan_id: str) -> None:
        """Finalize approval.

        Rule 5 (compliance dependency): reads ComplianceRecord state and injects it
        into the loan aggregate before applying ApplicationApproved.
        """
        loan = await self._load_loan(loan_id)
        compliance = await self._load_compliance(loan_id)

        # Lazy read: inject compliance state into loan aggregate
        loan.is_compliance_passed = compliance.is_passed

        event = BaseEvent(
            event_type="ApplicationApproved",
            payload={
                "application_id": loan_id,
                "approved_amount_usd": 50000.0,
                "interest_rate": 0.05,
                "conditions": [],
                "approved_by": "system",
                "effective_date": "2026-03-18",
            },
        )

        # Triggers Rule 5 guard in aggregate
        loan.apply(event)

        await self.store.append(f"loan-{loan_id}", [event], expected_version=loan.version)

    async def finalize_decline(self, loan_id: str) -> None:
        """Finalize a declined application."""
        loan = await self._load_loan(loan_id)
        event = BaseEvent(
            event_type="ApplicationDeclined",
            payload={"application_id": loan_id},
        )
        await self.store.append(f"loan-{loan_id}", [event], expected_version=loan.version)

    async def run_audit_integrity_check(self, loan_id: str) -> tuple[str, str]:
        """Run a cryptographic integrity check on the loan's audit stream.

        Appends an AuditIntegrityCheckRun event to audit-loan-{loan_id} and
        returns (new_integrity_hash, previous_hash).
        """
        return await run_integrity_check(f"audit-loan-{loan_id}", self.store)
