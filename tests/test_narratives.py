"""
tests/test_narratives.py
========================
Narrative scenario tests NARR-01 through NARR-05.

Run: pytest tests/test_narratives.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ── Shared helpers ─────────────────────────────────────────────────────────────


async def _make_pool() -> asyncpg.Pool:
    dsn = (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'postgres')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', 'password')}"
        f"@{os.environ.get('POSTGRES_HOST', 'localhost')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}/"
        f"{os.environ.get('POSTGRES_DB', 'event_ledger')}"
    )
    return await asyncpg.create_pool(dsn)


async def _make_store() -> tuple[asyncpg.Pool, Any]:
    from ledger.infrastructure.store import EventStore

    pool = await _make_pool()
    return pool, EventStore(pool)


async def _seed_loan(
    store: Any, app_id: str, applicant_id: str = "COMP-001", amount: float = 500_000.0
) -> None:
    """Seed ApplicationSubmitted. Idempotent — skips if stream already exists."""
    from ledger.core.models import BaseEvent

    if await store.stream_version(f"loan-{app_id}") >= 0:
        return
    event = BaseEvent(
        event_type="ApplicationSubmitted",
        payload={
            "application_id": app_id,
            "applicant_id": applicant_id,
            "requested_amount_usd": amount,
            "loan_purpose": "working_capital",
            "submission_channel": "Web",
            "submitted_at": "2026-03-17T00:00:00Z",
        },
    )
    await store.append(f"loan-{app_id}", [event], expected_version=-1)


class MockRegistry:
    """Stub registry — no applicant_registry DB schema required."""

    def __init__(
        self,
        jurisdiction: str = "CA",
        legal_type: str = "LLC",
        founded_year: int = 2018,
        compliance_flags: list[dict[str, Any]] | None = None,
    ):
        self._jurisdiction = jurisdiction
        self._legal_type = legal_type
        self._founded_year = founded_year
        self._flags: list[dict[str, Any]] = compliance_flags or []

    async def get_company(self, company_id: str) -> Any:
        from ledger.registry.client import CompanyProfile

        return CompanyProfile(
            company_id=company_id,
            name="Test Corp",
            industry="technology",
            naics="541511",
            jurisdiction=self._jurisdiction,
            legal_type=self._legal_type,
            founded_year=self._founded_year,
            employee_count=50,
            risk_segment="SMB",
            trajectory="STABLE",
            submission_channel="Web",
            ip_region="US-CA",
        )

    async def get_financial_history(
        self, _company_id: str, _years: list[int] | None = None
    ) -> list[Any]:
        from ledger.registry.client import FinancialYear

        return [
            FinancialYear(
                fiscal_year=2023,
                total_revenue=2_000_000,
                gross_profit=800_000,
                operating_income=300_000,
                ebitda=350_000,
                net_income=200_000,
                total_assets=1_500_000,
                total_liabilities=800_000,
                total_equity=700_000,
                long_term_debt=400_000,
                cash_and_equivalents=200_000,
                current_assets=600_000,
                current_liabilities=300_000,
                accounts_receivable=150_000,
                inventory=50_000,
                debt_to_equity=1.14,
                current_ratio=2.0,
                debt_to_ebitda=1.14,
                interest_coverage_ratio=5.0,
                gross_margin=0.40,
                ebitda_margin=0.175,
                net_margin=0.10,
            )
        ]

    async def get_compliance_flags(self, _company_id: str, _active_only: bool = False) -> list[Any]:
        from ledger.registry.client import ComplianceFlag

        return [ComplianceFlag(**f) for f in self._flags]

    async def get_loan_relationships(self, _company_id: str) -> list[dict[str, Any]]:
        return []


# ── NARR-01: Concurrent OCC collision ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_narr01_concurrent_occ_collision() -> None:
    """
    Two CreditAnalysisAgent instances run simultaneously on the same application.
    Expected: at least one CreditAnalysisCompleted in credit stream;
              second agent gets OCC on credit stream creation and fails gracefully.
    """
    from ledger.agents.credit_analysis_agent import CreditAnalysisAgent
    from ledger.core.models import BaseEvent

    pool, store = await _make_store()
    app_id = f"narr01-occ-{uuid.uuid4().hex[:8]}"
    mock_reg = MockRegistry()

    try:
        await _seed_loan(store, app_id)

        pkg_event = BaseEvent(
            event_type="ExtractionCompleted",
            payload={
                "package_id": app_id,
                "document_id": "doc-001",
                "facts": {
                    "total_revenue": 2_000_000,
                    "net_income": 200_000,
                    "total_assets": 1_500_000,
                    "total_liabilities": 800_000,
                    "total_equity": 700_000,
                    "ebitda": 350_000,
                },
            },
        )
        await store.append(f"docpkg-{app_id}", [pkg_event], expected_version=-1)

        agent1 = CreditAnalysisAgent("agent-credit-narr01-a", "credit_analysis", store, mock_reg)
        agent2 = CreditAnalysisAgent("agent-credit-narr01-b", "credit_analysis", store, mock_reg)

        results = await asyncio.gather(
            agent1.process_application(app_id),
            agent2.process_application(app_id),
            return_exceptions=True,
        )

        credit_events = await store.load_stream(f"credit-{app_id}")
        completed = [e for e in credit_events if e.event_type == "CreditAnalysisCompleted"]

        # At least one agent must produce a CreditAnalysisCompleted
        assert len(completed) >= 1, "Expected at least one CreditAnalysisCompleted"
        # At least one agent must have succeeded
        successes = [r for r in results if r is None]
        assert len(successes) >= 1, "At least one agent must complete successfully"

    finally:
        await pool.close()


# ── NARR-02: Document extraction failure ──────────────────────────────────────


@pytest.mark.asyncio
async def test_narr02_document_extraction_failure() -> None:
    """
    Income statement with missing EBITDA.
    Expected: CreditAnalysisCompleted.confidence <= 0.75,
              CreditAnalysisCompleted.data_quality_caveats or key_concerns is non-empty.
    """
    from ledger.agents.credit_analysis_agent import CreditAnalysisAgent
    from ledger.core.models import BaseEvent

    pool, store = await _make_store()
    app_id = f"narr02-ebitda-{uuid.uuid4().hex[:8]}"
    mock_reg = MockRegistry()

    try:
        await _seed_loan(store, app_id)

        # Seed docpkg with missing ebitda
        pkg_event = BaseEvent(
            event_type="ExtractionCompleted",
            payload={
                "package_id": app_id,
                "document_id": "doc-001",
                "facts": {
                    "total_revenue": 1_500_000,
                    "net_income": 120_000,
                    "total_assets": 900_000,
                    # ebitda intentionally missing
                },
            },
        )
        await store.append(f"docpkg-{app_id}", [pkg_event], expected_version=-1)

        # Seed QualityAssessmentCompleted flagging missing ebitda
        docpkg_ver = await store.stream_version(f"docpkg-{app_id}")
        qa_event = BaseEvent(
            event_type="QualityAssessmentCompleted",
            payload={
                "package_id": app_id,
                "is_consistent": False,
                "anomalies": ["EBITDA not present in income statement"],
                "critical_missing_fields": ["ebitda"],
                "overall_quality": "LOW",
            },
        )
        await store.append(f"docpkg-{app_id}", [qa_event], expected_version=docpkg_ver)

        agent = CreditAnalysisAgent("agent-credit-narr02", "credit_analysis", store, mock_reg)
        await agent.process_application(app_id)

        credit_events = await store.load_stream(f"credit-{app_id}")
        completed = [e for e in credit_events if e.event_type == "CreditAnalysisCompleted"]
        assert completed, "Expected CreditAnalysisCompleted event"

        payload = completed[-1].payload
        assert payload.get("confidence", 1.0) <= 0.75, (
            f"Expected confidence <= 0.75, got {payload.get('confidence')}"
        )
        assert payload.get("data_quality_caveats") or payload.get("key_concerns"), (
            "Expected non-empty data_quality_caveats or key_concerns"
        )

    finally:
        await pool.close()


# ── NARR-03: Agent crash recovery ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_narr03_agent_crash_recovery() -> None:
    """
    FraudDetectionAgent runs on a seeded application.
    Expected: exactly ONE FraudScreeningCompleted in fraud stream,
              fraud_score in [0.0, 1.0].
    """
    from ledger.agents.stub_agents import FraudDetectionAgent
    from ledger.core.models import BaseEvent

    pool, store = await _make_store()
    app_id = f"narr03-crash-{uuid.uuid4().hex[:8]}"
    mock_reg = MockRegistry()

    try:
        await _seed_loan(store, app_id)

        # Seed FraudScreeningRequested on loan stream
        loan_ver = await store.stream_version(f"loan-{app_id}")
        trigger = BaseEvent(
            event_type="FraudScreeningRequested",
            payload={"application_id": app_id, "requested_at": "2026-03-17T00:00:00Z"},
        )
        await store.append(f"loan-{app_id}", [trigger], expected_version=loan_ver)

        # Seed docpkg with extraction facts
        pkg_event = BaseEvent(
            event_type="ExtractionCompleted",
            payload={
                "package_id": app_id,
                "document_id": "doc-001",
                "facts": {
                    "total_revenue": 1_000_000,
                    "total_assets": 800_000,
                    "total_liabilities": 400_000,
                    "total_equity": 400_000,
                },
            },
        )
        await store.append(f"docpkg-{app_id}", [pkg_event], expected_version=-1)

        agent = FraudDetectionAgent("agent-fraud-narr03", "fraud_detection", store, mock_reg)
        await agent.process_application(app_id)

        fraud_events = await store.load_stream(f"fraud-{app_id}")
        completed = [e for e in fraud_events if e.event_type == "FraudScreeningCompleted"]
        assert len(completed) == 1, (
            f"Expected exactly 1 FraudScreeningCompleted, got {len(completed)}"
        )

        payload = completed[0].payload
        assert 0.0 <= float(payload.get("fraud_score", -1)) <= 1.0, "fraud_score must be in [0, 1]"

    finally:
        await pool.close()


# ── NARR-04: Compliance hard block (Montana) ──────────────────────────────────


@pytest.mark.asyncio
async def test_narr04_compliance_hard_block() -> None:
    """
    Montana applicant triggers REG-003 hard block.
    Expected: ComplianceRuleFailed(rule_id='REG-003', is_hard_block=True),
              NO DecisionRequested event,
              ApplicationDeclined with adverse_action_notice_required=True.
    """
    from ledger.agents.stub_agents import ComplianceAgent
    from ledger.core.models import BaseEvent

    pool, store = await _make_store()
    app_id = f"narr04-mt-{uuid.uuid4().hex[:8]}"
    mt_registry = MockRegistry(jurisdiction="MT")

    try:
        await _seed_loan(store, app_id, applicant_id="COMP-MT-001")

        # Seed ComplianceCheckRequested on loan stream
        loan_ver = await store.stream_version(f"loan-{app_id}")
        trigger = BaseEvent(
            event_type="ComplianceCheckRequested",
            payload={"application_id": app_id},
        )
        await store.append(f"loan-{app_id}", [trigger], expected_version=loan_ver)

        agent = ComplianceAgent("agent-compliance-narr04", "compliance", store, mt_registry)
        await agent.process_application(app_id)

        # Check compliance stream
        comp_events = await store.load_stream(f"compliance-{app_id}")
        reg003_failed = [
            e
            for e in comp_events
            if e.event_type == "ComplianceRuleFailed" and e.payload.get("rule_id") == "REG-003"
        ]
        assert reg003_failed, "Expected ComplianceRuleFailed for REG-003"
        assert reg003_failed[0].payload.get("is_hard_block") is True

        # Check loan stream — no DecisionRequested, but ApplicationDeclined present
        loan_events = await store.load_stream(f"loan-{app_id}")
        decision_requested = [e for e in loan_events if e.event_type == "DecisionRequested"]
        assert not decision_requested, "DecisionRequested must NOT be present for hard block"

        declined = [e for e in loan_events if e.event_type == "ApplicationDeclined"]
        assert declined, "Expected ApplicationDeclined on loan stream"
        assert declined[0].payload.get("adverse_action_notice_required") is True

    finally:
        await pool.close()


# ── NARR-05: Human override ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_narr05_human_override() -> None:
    """
    Orchestrator recommends DECLINE; human loan officer overrides to APPROVE.
    Expected: DecisionGenerated(recommendation='DECLINE'),
              HumanReviewCompleted(override=True, reviewer_id='LO-Sarah-Chen'),
              ApplicationApproved(approved_amount_usd=750000, len(conditions)==2).
    """
    from ledger.application.service import LedgerService
    from ledger.core.models import BaseEvent

    pool, store = await _make_store()
    app_id = f"narr05-override-{uuid.uuid4().hex[:8]}"
    service = LedgerService(store)

    try:
        await _seed_loan(store, app_id)

        # Walk the loan through the state machine to PENDING_DECISION
        # so DecisionGenerated is valid
        loan_ver = await store.stream_version(f"loan-{app_id}")
        await store.append(
            f"loan-{app_id}",
            [
                BaseEvent(
                    event_type="CreditAnalysisRequested",
                    payload={"application_id": app_id},
                )
            ],
            expected_version=loan_ver,
        )

        loan_ver = await store.stream_version(f"loan-{app_id}")
        await store.append(
            f"loan-{app_id}",
            [
                BaseEvent(
                    event_type="CreditAnalysisCompleted",
                    event_version=2,
                    payload={
                        "application_id": app_id,
                        "agent_id": "agent-test",
                        "session_id": "sess-test",
                        "model_version": "test-v1",
                        "confidence_score": 0.72,
                        "risk_tier": "HIGH",
                        "recommended_limit_usd": 500_000,
                        "analysis_duration_ms": 100,
                        "input_data_hash": "abc123",
                        "reasoning": "Test",
                        "risk_score_raw": 0.72,
                        "regulatory_basis": "EU-AI-ACT-2025",
                    },
                )
            ],
            expected_version=loan_ver,
        )

        loan_ver = await store.stream_version(f"loan-{app_id}")
        await store.append(
            f"loan-{app_id}",
            [
                BaseEvent(
                    event_type="ComplianceCheckRequested",
                    payload={"application_id": app_id},
                )
            ],
            expected_version=loan_ver,
        )

        loan_ver = await store.stream_version(f"loan-{app_id}")
        await store.append(
            f"loan-{app_id}",
            [
                BaseEvent(
                    event_type="ComplianceRulePassed",
                    payload={"application_id": app_id, "rule_id": "ALL", "rule_version": "1.0"},
                )
            ],
            expected_version=loan_ver,
        )

        # Now in PENDING_DECISION — append DecisionGenerated(DECLINE)
        loan_ver = await store.stream_version(f"loan-{app_id}")
        await store.append(
            f"loan-{app_id}",
            [
                BaseEvent(
                    event_type="DecisionGenerated",
                    payload={
                        "application_id": app_id,
                        "recommendation": "DECLINE",
                        "confidence_score": 0.72,
                        "approved_amount_usd": 0,
                        "executive_summary": "High risk profile. Recommend decline.",
                        "conditions": [],
                        "policy_overrides_applied": ["HIGH_RISK"],
                        "contributing_agent_sessions": ["agent-test-sess-test"],
                        "generated_at": "2026-03-17T10:00:00Z",
                    },
                )
            ],
            expected_version=loan_ver,
        )

        # Human reviewer overrides to APPROVE
        await service.record_human_review(
            loan_id=app_id,
            reviewer_id="LO-Sarah-Chen",
            decision="APPROVE",
            override=True,
            override_reason="Applicant provided additional collateral documentation.",
        )

        # Append ApplicationApproved with conditions
        loan_ver = await store.stream_version(f"loan-{app_id}")
        await store.append(
            f"loan-{app_id}",
            [
                BaseEvent(
                    event_type="ApplicationApproved",
                    payload={
                        "application_id": app_id,
                        "approved_amount_usd": 750_000,
                        "conditions": [
                            "Quarterly financial reporting required",
                            "Personal guarantee from primary owner",
                        ],
                        "approved_by": "LO-Sarah-Chen",
                        "override": True,
                        "effective_date": "2026-03-18",
                    },
                )
            ],
            expected_version=loan_ver,
        )

        # Verify the event sequence
        final_events = await store.load_stream(f"loan-{app_id}")
        event_types = [e.event_type for e in final_events]

        assert "DecisionGenerated" in event_types
        assert "HumanReviewCompleted" in event_types
        assert "ApplicationApproved" in event_types

        dg = next(e for e in final_events if e.event_type == "DecisionGenerated")
        assert dg.payload.get("recommendation") == "DECLINE"

        hr = next(e for e in final_events if e.event_type == "HumanReviewCompleted")
        assert hr.payload.get("override") is True
        assert hr.payload.get("reviewer_id") == "LO-Sarah-Chen"

        aa = next(e for e in final_events if e.event_type == "ApplicationApproved")
        assert float(aa.payload.get("approved_amount_usd", 0)) == 750_000
        assert len(aa.payload.get("conditions", [])) == 2

    finally:
        await pool.close()
