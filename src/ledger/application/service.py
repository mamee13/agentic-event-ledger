"""Command handlers for The Ledger.

Pattern: load aggregates -> validate rules -> build event -> append with expected_version.

Stream ID formats:
  loan-{application_id}
  agent-{agent_id}-{session_id}
  compliance-{application_id}
  audit-{entity_type}-{entity_id}
"""

from datetime import UTC, datetime
from typing import Any

from ledger.core.aggregates import (
    AgentSessionAggregate,
    ComplianceRecordAggregate,
    LoanApplicationAggregate,
)
from ledger.core.audit_chain import run_integrity_check
from ledger.core.models import BaseEvent
from ledger.infrastructure.store import EventStore


class LedgerService:
    def __init__(self, store: EventStore):
        self.store = store

    # ------------------------------------------------------------------ audit helpers

    def _audit_stream_id(self, loan_id: str) -> str:
        return f"audit-loan-{loan_id}"

    def _extract_application_id(self, events: list[BaseEvent]) -> str | None:
        app_ids = {
            e.payload.get("application_id") for e in events if e.payload.get("application_id")
        }
        if not app_ids:
            return None
        if len(app_ids) > 1:
            raise ValueError(f"Multiple application_id values found in events: {app_ids}")
        return str(next(iter(app_ids)))

    def _build_audit_events(
        self, writes: list[tuple[str, list[BaseEvent], int]]
    ) -> list[BaseEvent]:
        audit_events: list[BaseEvent] = []
        for stream_id, events, _ in writes:
            for event in events:
                app_id = event.payload.get("application_id")
                if not app_id:
                    continue
                audit_events.append(
                    BaseEvent(
                        event_type=event.event_type,
                        event_version=event.event_version,
                        payload=event.payload,
                        metadata={**event.metadata, "source_stream_id": stream_id},
                    )
                )
        return audit_events

    async def _append_with_audit(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: str | None,
        causation_id: str | None,
    ) -> None:
        writes = [(stream_id, events, expected_version)]
        await self._append_multi_with_audit(writes, correlation_id, causation_id)

    async def _append_multi_with_audit(
        self,
        writes: list[tuple[str, list[BaseEvent], int]],
        correlation_id: str | None,
        causation_id: str | None,
    ) -> None:
        audit_events = self._build_audit_events(writes)
        if not audit_events:
            await self.store.append_multi(
                writes, correlation_id=correlation_id, causation_id=causation_id
            )
            return

        app_id = self._extract_application_id(audit_events)
        if not app_id:
            await self.store.append_multi(
                writes, correlation_id=correlation_id, causation_id=causation_id
            )
            return

        audit_stream_id = self._audit_stream_id(app_id)
        audit_expected = await self.store.stream_version(audit_stream_id)

        audit_write = (audit_stream_id, audit_events, audit_expected if audit_expected >= 0 else -1)
        await self.store.append_multi(
            [*writes, audit_write],
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

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

    async def submit_application(
        self,
        loan_id: str,
        amount: float,
        applicant_id: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
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
        await self._append_with_audit(
            f"loan-{loan_id}",
            [event],
            expected_version=-1,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def record_document_upload(
        self,
        loan_id: str,
        document_id: str,
        file_path: str,
        document_type: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Record a document upload event to the loan stream."""
        event = BaseEvent(
            event_type="DocumentUploaded",
            payload={
                "application_id": loan_id,
                "document_id": document_id,
                "file_path": file_path,
                "document_type": document_type,
                "uploaded_at": datetime.now(UTC).isoformat(),
            },
        )
        loan = await self._load_loan(loan_id)
        await self._append_with_audit(
            f"loan-{loan_id}",
            [event],
            expected_version=loan.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def start_agent_session(
        self,
        session_id: str,
        agent_id: str,
        model_version: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
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
        await self._append_with_audit(
            f"agent-{agent_id}-{session_id}",
            [event],
            expected_version=-1,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def request_credit_analysis(
        self,
        loan_id: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Transition loan to AWAITING_ANALYSIS."""
        loan = await self._load_loan(loan_id)
        event = BaseEvent(event_type="CreditAnalysisRequested", payload={"application_id": loan_id})
        await self._append_with_audit(
            f"loan-{loan_id}",
            [event],
            expected_version=loan.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def record_credit_analysis(
        self,
        loan_id: str,
        agent_id: str,
        session_id: str,
        risk_score: float,
        reasoning: str,
        analysis_duration_ms: int = 0,
        risk_tier: str = "MEDIUM",
        confidence_score: float = 0.85,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Record a completed credit analysis on the loan stream AND the agent session stream."""
        # 1. Load
        session = await self._load_session(agent_id, session_id)
        loan = await self._load_loan(loan_id)

        # 2. Validate
        session.guard_context_loaded()
        loan.guard_record_credit_analysis()

        # 3. Determine
        event = BaseEvent(
            event_type="CreditAnalysisCompleted",
            event_version=2,
            payload={
                "application_id": loan_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "model_version": session.model_version,
                "confidence_score": confidence_score,
                "risk_tier": risk_tier,
                "recommended_limit_usd": 50000.0,
                "analysis_duration_ms": analysis_duration_ms,
                "input_data_hash": "sha256-abc123",
                "reasoning": reasoning,
                "risk_score_raw": risk_score,
                "regulatory_basis": "EU-AI-ACT-2025",
            },
        )

        # Capture versions before append
        loan_version = loan.version
        session_version = session.version

        # 4. Append both streams atomically — one transaction, no partial-write risk
        await self._append_multi_with_audit(
            [
                (f"loan-{loan_id}", [event], loan_version),
                (f"agent-{agent_id}-{session_id}", [event], session_version),
            ],
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def request_compliance_check(
        self,
        loan_id: str,
        required_rules: list[str] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Transition loan to COMPLIANCE_REVIEW and open the compliance stream.

        ComplianceCheckRequested goes to BOTH streams:
          - loan stream: drives the loan state machine to COMPLIANCE_REVIEW
          - compliance stream: opens the compliance record for rule results
        """
        loan = await self._load_loan(loan_id)
        compliance = await self._load_compliance(loan_id)

        payload: dict[str, Any] = {"application_id": loan_id}
        if required_rules:
            payload["required_rules"] = required_rules

        loan_event = BaseEvent(event_type="ComplianceCheckRequested", payload=payload)
        comp_event = BaseEvent(event_type="ComplianceCheckRequested", payload=payload)
        comp_expected = compliance.version if compliance.version >= 0 else -1
        await self._append_multi_with_audit(
            [
                (f"loan-{loan_id}", [loan_event], loan.version),
                (f"compliance-{loan_id}", [comp_event], comp_expected),
            ],
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def record_fraud_screening(
        self,
        loan_id: str,
        agent_id: str,
        session_id: str,
        fraud_score: float,
        screening_result: str,
        flags: list[str],
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Record a completed fraud screening to both loan and agent session streams."""
        session = await self._load_session(agent_id, session_id)
        loan = await self._load_loan(loan_id)

        session.guard_context_loaded()

        event = BaseEvent(
            event_type="FraudScreeningCompleted",
            payload={
                "application_id": loan_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "fraud_score": fraud_score,
                "screening_result": screening_result,
                "flags": flags,
            },
        )

        await self._append_multi_with_audit(
            [
                (f"loan-{loan_id}", [event], loan.version),
                (f"agent-{agent_id}-{session_id}", [event], session.version),
            ],
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def record_compliance(
        self,
        loan_id: str,
        rule_id: str,
        status: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Record a compliance rule result.

        ComplianceRulePassed / ComplianceRuleFailed go to the compliance stream.
        When all rules pass, a ComplianceRulePassed event is also appended to the
        loan stream so the loan state machine advances from COMPLIANCE_REVIEW to
        PENDING_DECISION.
        """
        # 1. Load & Validate
        compliance = await self._load_compliance(loan_id)
        loan = await self._load_loan(loan_id)
        loan.guard_record_compliance()

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
        await self._append_with_audit(
            f"compliance-{loan_id}",
            [event],
            expected_version=comp_expected,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

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
            await self._append_with_audit(
                f"loan-{loan_id}",
                [loan_clearance],
                expected_version=loan_version,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )

    async def generate_decision(
        self,
        loan_id: str,
        agent_id: str,
        session_id: str,
        recommendation: str,
        confidence: float,
        contributing_sessions: list[dict[str, str]],
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Generate a decision for a loan application."""
        # 1. Load
        session = await self._load_session(agent_id, session_id)
        loan = await self._load_loan(loan_id)
        linked: list[AgentSessionAggregate] = []
        for contrib in contributing_sessions:
            c_stream = f"agent-{contrib['agent_id']}-{contrib['session_id']}"
            c_session = await AgentSessionAggregate.load(c_stream, self.store)
            linked.append(c_session)

        # 2. Validate
        session.guard_context_loaded()
        loan.validate_causal_chain(linked)

        # Build payload for guard and event construction
        model_versions: dict[str, str] = {
            c_sess.agent_id: c_sess.model_version
            for c_sess in linked
            if c_sess.agent_id and c_sess.model_version
        }
        model_versions[agent_id] = session.model_version or "unknown"

        payload = {
            "application_id": loan_id,
            "orchestrator_agent_id": agent_id,
            "recommendation": recommendation,
            "confidence_score": confidence,
            "contributing_agent_sessions": [
                f"agent-{c['agent_id']}-{c['session_id']}" for c in contributing_sessions
            ],
            "decision_basis_summary": "All agents agree",
            "model_versions": model_versions,
        }

        loan.guard_generate_decision(payload)

        # 3. Determine
        event = BaseEvent(
            event_type="DecisionGenerated",
            event_version=2,
            payload=payload,
        )

        # Capture versions
        loan_version = loan.version
        session_version = session.version

        # 4. Append both streams atomically — one transaction, no partial-write risk
        await self._append_multi_with_audit(
            [
                (f"loan-{loan_id}", [event], loan_version),
                (f"agent-{agent_id}-{session_id}", [event], session_version),
            ],
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def record_human_review(
        self,
        loan_id: str,
        reviewer_id: str,
        decision: str,
        override: bool = False,
        override_reason: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Record a human reviewer's decision."""
        # 1. Load
        loan = await self._load_loan(loan_id)

        # 2. Validate
        loan.guard_human_review(override, override_reason)
        if override and not override_reason:
            from ledger.core.errors import DomainRuleError

            raise DomainRuleError(
                rule_name="human_review",
                message="override_reason is required when override=True",
                suggested_action="Provide a reason for the override.",
            )

        # 3. Determine
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

        # 4. Append
        await self._append_with_audit(
            f"loan-{loan_id}",
            [event],
            expected_version=loan.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def finalize_approval(
        self,
        loan_id: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Finalize approval."""
        # 1. Load
        loan = await self._load_loan(loan_id)
        compliance = await self._load_compliance(loan_id)

        # 2. Validate
        loan.guard_finalize_approval(compliance.is_passed)

        # 3. Determine
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

        # 4. Append
        await self._append_with_audit(
            f"loan-{loan_id}",
            [event],
            expected_version=loan.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def finalize_decline(
        self,
        loan_id: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        """Finalize a declined application."""
        loan = await self._load_loan(loan_id)
        event = BaseEvent(
            event_type="ApplicationDeclined",
            payload={"application_id": loan_id},
        )
        await self._append_with_audit(
            f"loan-{loan_id}",
            [event],
            expected_version=loan.version,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )

    async def run_audit_integrity_check(self, loan_id: str) -> tuple[str, str]:
        """Run a cryptographic integrity check on the loan's audit stream.

        Appends an AuditIntegrityCheckRun event to audit-loan-{loan_id} and
        returns (new_integrity_hash, previous_hash).
        """
        return await run_integrity_check(f"audit-loan-{loan_id}", self.store)
