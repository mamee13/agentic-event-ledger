from ledger.core.aggregates import AgentSessionAggregate, LoanApplicationAggregate
from ledger.core.models import BaseEvent
from ledger.infrastructure.store import EventStore


class LedgerService:
    def __init__(self, store: EventStore):
        self.store = store

    async def submit_application(self, loan_id: str, amount: float, applicant_id: str) -> None:
        """Handles the initial application submission."""
        # 1. Check if already exists (optional, store will handle unique violation if needed)
        # 2. Build event
        event = BaseEvent(
            event_type="ApplicationSubmitted",
            payload={"loan_id": loan_id, "amount": amount, "applicant_id": applicant_id},
        )
        # 3. Append to new stream
        await self.store.append(loan_id, [event], expected_version=-1)

    async def start_agent_session(self, session_id: str, agent_id: str, model_version: str) -> None:
        """Starts a new agent session with context."""
        event = BaseEvent(
            event_type="AgentContextLoaded",
            payload={"agent_id": agent_id, "model_version": model_version},
        )
        await self.store.append(session_id, [event], expected_version=-1)

    async def record_credit_analysis(
        self, loan_id: str, session_id: str, risk_score: float, reasoning: str
    ) -> None:
        """Appends a credit analysis to a loan application."""
        # 1. Load Session Aggregate to verify it's active
        session_events = await self.store.load_stream(session_id)
        if not session_events:
            raise ValueError(f"Session {session_id} not found")

        session = AgentSessionAggregate(session_id)
        session.load_from_history(session_events)

        # 2. Load Loan Aggregate
        loan_events = await self.store.load_stream(loan_id)
        loan = LoanApplicationAggregate(loan_id)
        loan.load_from_history(loan_events)

        # 3. Model-version locking: reject if this model already analyzed the loan
        if session.agent_id in loan.model_versions:
            raise ValueError(f"Model {session.agent_id} has already analyzed loan {loan_id}")

        # 4. Build analysis event
        event = BaseEvent(
            event_type="CreditAnalysisCompleted",
            payload={
                "risk_score": risk_score,
                "reasoning": reasoning,
                "session_id": session_id,
                "model_version": session.agent_id,  # Simplified for now
            },
        )

        # 4. Append
        await self.store.append(loan_id, [event], expected_version=loan.version)

    async def start_evaluation(self, loan_id: str) -> None:
        """Transitions a loan to Under Review."""
        loan_events = await self.store.load_stream(loan_id)
        loan = LoanApplicationAggregate(loan_id)
        loan.load_from_history(loan_events)

        if loan.state != "SUBMITTED":
            raise ValueError("Loan must be in SUBMITTED state to start evaluation")

        event = BaseEvent(event_type="EvaluationStarted", payload={})
        await self.store.append(loan_id, [event], expected_version=loan.version)

    async def record_compliance(self, loan_id: str, rule_id: str, status: str) -> None:
        """Records a compliance check result."""
        loan_events = await self.store.load_stream(loan_id)
        loan = LoanApplicationAggregate(loan_id)
        loan.load_from_history(loan_events)

        event_type = "ComplianceCheckPassed" if status == "PASSED" else "ComplianceCheckFailed"
        event = BaseEvent(event_type=event_type, payload={"rule_id": rule_id})
        await self.store.append(loan_id, [event], expected_version=loan.version)

    async def record_human_review(
        self, loan_id: str, reviewer_id: str, decision: str, override_reason: str | None = None
    ) -> None:
        """Records a human review decision, potentially overriding an AI decision."""
        loan_events = await self.store.load_stream(loan_id)
        loan = LoanApplicationAggregate(loan_id)
        loan.load_from_history(loan_events)

        event = BaseEvent(
            event_type="HumanReviewCompleted",
            payload={
                "reviewer_id": reviewer_id,
                "decision": decision,
                "override_reason": override_reason,
            },
        )
        await self.store.append(loan_id, [event], expected_version=loan.version)

    async def generate_decision(
        self, loan_id: str, session_id: str, decision: str, confidence: float
    ) -> None:
        """Generates a decision for a loan application."""
        # 1. Load Loan
        loan_events = await self.store.load_stream(loan_id)
        loan = LoanApplicationAggregate(loan_id)
        loan.load_from_history(loan_events)

        # 2. Validate compliance checks if decision is APPROVE
        if decision == "APPROVE" and not loan.compliance_passed:
            raise ValueError("Compliance checks must pass before approval")

        # 3. Build decision event
        event = BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "decision": decision,
                "confidence_score": confidence,
                "session_id": session_id,
            },
        )

        # 4. Append
        await self.store.append(loan_id, [event], expected_version=loan.version)
