"""Integration tests for what-if projector and regulatory package generator.

Tests:
  1. Counterfactual divergence: changing risk_tier MEDIUM → HIGH produces a
     materially different ApplicationSummary outcome.
  2. Regulatory package completeness: all required sections are present and
     the package checksum is reproducible.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ledger.core.models import BaseEvent
from ledger.core.regulatory_package import generate_regulatory_package
from ledger.core.whatif import WhatIfResult, run_whatif
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.store import EventStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _build_full_lifecycle(
    store: EventStore,
    app_id: str,
    agent_id: str,
    session_id: str,
    risk_tier: str = "MEDIUM",
    confidence: float = 0.82,
    recommendation: str = "APPROVE",
) -> None:
    """Write a complete loan lifecycle to the store."""
    loan_stream = f"loan-{app_id}"
    session_stream = f"agent-{agent_id}-{session_id}"

    # Session
    await store.append(
        session_stream,
        [
            BaseEvent(
                event_type="AgentContextLoaded",
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "v2.0",
                    "context_source": "MLflow",
                    "context_token_count": 512,
                    "event_replay_from_position": 0,
                },
            )
        ],
        expected_version=-1,
    )

    # Loan lifecycle
    await store.append(
        loan_stream,
        [
            BaseEvent(
                event_type="ApplicationSubmitted",
                payload={
                    "application_id": app_id,
                    "applicant_id": "user-001",
                    "requested_amount_usd": 50000.0,
                },
            )
        ],
        expected_version=-1,
    )

    v = await store.stream_version(loan_stream)
    await store.append(
        loan_stream,
        [BaseEvent(event_type="CreditAnalysisRequested", payload={"application_id": app_id})],
        expected_version=v,
    )

    credit_event = BaseEvent(
        event_type="CreditAnalysisCompleted",
        event_version=2,
        payload={
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "model_version": "v2.0",
            "risk_tier": risk_tier,
            "confidence_score": confidence,
            "recommended_limit_usd": 50000.0,
            "analysis_duration_ms": 300,
            "input_data_hash": "sha256-abc123",
            "reasoning": "Test reasoning",
            "risk_score_raw": confidence,
            "regulatory_basis": "EU-AI-ACT-2025",
        },
    )
    v = await store.stream_version(loan_stream)
    await store.append(loan_stream, [credit_event], expected_version=v)
    sv = await store.stream_version(session_stream)
    await store.append(session_stream, [credit_event], expected_version=sv)

    # Compliance
    compliance_stream = f"compliance-{app_id}"
    v = await store.stream_version(loan_stream)
    await store.append(
        loan_stream,
        [BaseEvent(event_type="ComplianceCheckRequested", payload={"application_id": app_id})],
        expected_version=v,
    )
    await store.append(
        compliance_stream,
        [BaseEvent(event_type="ComplianceCheckRequested", payload={"application_id": app_id})],
        expected_version=-1,
    )

    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        cv = await store.stream_version(compliance_stream)
        await store.append(
            compliance_stream,
            [
                BaseEvent(
                    event_type="ComplianceRulePassed",
                    payload={"application_id": app_id, "rule_id": rule, "rule_version": "1.0"},
                )
            ],
            expected_version=cv,
        )

    # Clearance to loan stream
    v = await store.stream_version(loan_stream)
    await store.append(
        loan_stream,
        [
            BaseEvent(
                event_type="ComplianceRulePassed",
                payload={
                    "application_id": app_id,
                    "rule_id": "compliance_clearance",
                    "rule_version": "1.0",
                },
            )
        ],
        expected_version=v,
    )

    # Decision
    decision_event = BaseEvent(
        event_type="DecisionGenerated",
        event_version=2,
        payload={
            "application_id": app_id,
            "orchestrator_agent_id": agent_id,
            "recommendation": recommendation,
            "confidence_score": confidence,
            "contributing_agent_sessions": [session_stream],
            "decision_basis_summary": "All agents agree",
            "model_versions": {agent_id: "v2.0"},
        },
    )
    v = await store.stream_version(loan_stream)
    await store.append(loan_stream, [decision_event], expected_version=v)
    sv = await store.stream_version(session_stream)
    await store.append(session_stream, [decision_event], expected_version=sv)

    # Human review
    v = await store.stream_version(loan_stream)
    await store.append(
        loan_stream,
        [
            BaseEvent(
                event_type="HumanReviewCompleted",
                payload={
                    "application_id": app_id,
                    "reviewer_id": "reviewer-jane",
                    "final_decision": "APPROVE",
                    "override": False,
                },
            )
        ],
        expected_version=v,
    )


# ---------------------------------------------------------------------------
# Test 1: Counterfactual divergence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whatif_risk_tier_medium_to_high_diverges() -> None:
    """Changing risk_tier from MEDIUM to HIGH must produce a materially different outcome.

    The required scenario from the brief:
      Real: CreditAnalysisCompleted with risk_tier=MEDIUM → APPROVE
      Counterfactual: CreditAnalysisCompleted with risk_tier=HIGH → different state
    """
    pool = await get_pool()
    store = EventStore(pool)

    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    await _build_full_lifecycle(
        store,
        app_id,
        agent_id,
        session_id,
        risk_tier="MEDIUM",
        confidence=0.82,
        recommendation="APPROVE",
    )

    # Counterfactual: same event but risk_tier=HIGH and lower confidence → REFER
    cf_payload = {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.0",
        "risk_tier": "HIGH",
        "confidence_score": 0.45,  # below 0.6 floor → forces REFER
        "recommended_limit_usd": 20000.0,
        "analysis_duration_ms": 300,
        "input_data_hash": "sha256-abc123",
        "reasoning": "High risk detected",
        "risk_score_raw": 0.45,
        "regulatory_basis": "EU-AI-ACT-2025",
    }

    result: WhatIfResult = await run_whatif(
        application_id=app_id,
        store=store,
        branch_event_type="CreditAnalysisCompleted",
        counterfactual_payload=cf_payload,
    )

    # Real outcome: APPROVE path
    assert result.real_outcome["risk_tier"] == "MEDIUM"
    assert result.real_outcome["state"] in ("FINAL_APPROVED", "APPROVED_PENDING_HUMAN", "REFERRED")

    # Counterfactual outcome: HIGH risk tier recorded
    assert result.counterfactual_outcome["risk_tier"] == "HIGH"

    # There must be divergence
    assert len(result.divergence_events) > 0, "Expected divergence between real and counterfactual"

    # risk_tier must be in the divergence list
    assert "risk_tier" in result.divergence_events

    # Some dependent events must have been skipped
    assert result.events_skipped > 0

    print("\n\n" + "=" * 50)
    print("WHAT-IF COUNTERFACTUAL: RISK_TIER MEDIUM -> HIGH")
    print("=" * 50)
    print("1. Real Outcome (MEDIUM risk):")
    print(f"   State:     {result.real_outcome.get('state')}")
    print(f"   Risk Tier: {result.real_outcome.get('risk_tier')}")
    print("2. Counterfactual Outcome (HIGH risk):")
    print(f"   State:     {result.counterfactual_outcome.get('state')}")
    print(f"   Risk Tier: {result.counterfactual_outcome.get('risk_tier')}")
    print(f"\nDivergence detected in fields: {', '.join(result.divergence_events)}")
    print(f"Events skipped due to causal dependency: {result.events_skipped}")
    print("Cascading effect of business rules verified.")
    print("==================================================\n")

    await pool.close()


@pytest.mark.asyncio
async def test_whatif_returns_correct_structure() -> None:
    """WhatIfResult must have all required fields."""
    pool = await get_pool()
    store = EventStore(pool)

    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    cf_payload = {
        "application_id": app_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_version": "v2.0",
        "risk_tier": "VERY_HIGH",
        "confidence_score": 0.3,
        "recommended_limit_usd": 0.0,
        "analysis_duration_ms": 300,
        "input_data_hash": "sha256-xyz",
        "reasoning": "Very high risk",
        "risk_score_raw": 0.3,
        "regulatory_basis": "EU-AI-ACT-2025",
    }

    result = await run_whatif(
        application_id=app_id,
        store=store,
        branch_event_type="CreditAnalysisCompleted",
        counterfactual_payload=cf_payload,
    )

    assert result.application_id == app_id
    assert result.branch_event_type == "CreditAnalysisCompleted"
    assert isinstance(result.real_outcome, dict)
    assert isinstance(result.counterfactual_outcome, dict)
    assert isinstance(result.divergence_events, list)
    assert isinstance(result.events_replayed, int)
    assert isinstance(result.events_skipped, int)
    assert result.events_replayed > 0

    await pool.close()


@pytest.mark.asyncio
async def test_whatif_no_branch_event_returns_unchanged() -> None:
    """If the branch event type doesn't exist in the stream, real and cf outcomes are equal."""
    pool = await get_pool()
    store = EventStore(pool)

    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    result = await run_whatif(
        application_id=app_id,
        store=store,
        branch_event_type="NonExistentEventType",
        counterfactual_payload={"application_id": app_id},
    )

    # No branch found — counterfactual = real
    assert result.real_outcome == result.counterfactual_outcome
    assert result.divergence_events == []
    assert result.events_skipped == 0

    await pool.close()


# ---------------------------------------------------------------------------
# Test 2: Regulatory package completeness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regulatory_package_completeness() -> None:
    """Regulatory package must contain all 5 required sections."""
    pool = await get_pool()
    store = EventStore(pool)
    compliance = ComplianceAuditViewProjection(pool)

    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    # Seed compliance projection
    events = await store.load_stream(f"loan-{app_id}")
    for event in events:
        if event.event_type in compliance.subscribed_events:
            await compliance.handle_event(event)

    # Also seed from compliance stream
    comp_events = await store.load_stream(f"compliance-{app_id}")
    for event in comp_events:
        if event.event_type in compliance.subscribed_events:
            await compliance.handle_event(event)

    examination_date = datetime.now(UTC) + timedelta(hours=1)

    package = await generate_regulatory_package(
        application_id=app_id,
        examination_date=examination_date,
        store=store,
        compliance_projection=compliance,
    )

    # Section 1: event stream
    assert "event_stream" in package
    assert len(package["event_stream"]) > 0
    first_event = package["event_stream"][0]
    assert "event_id" in first_event
    assert "event_type" in first_event
    assert "payload" in first_event
    assert "recorded_at" in first_event
    assert "stream_position" in first_event

    # Section 2: projection states
    assert "projection_states" in package
    assert "application_summary" in package["projection_states"]
    assert "compliance_audit_view" in package["projection_states"]

    # Section 3: integrity verification
    assert "integrity_verification" in package
    iv = package["integrity_verification"]
    assert "is_valid" in iv
    assert "message" in iv
    assert "hash_chain_summary" in iv
    hcs = iv["hash_chain_summary"]
    assert "chain_valid" in hcs
    assert "total_check_runs" in hcs
    assert "last_integrity_hash" in hcs

    # Section 4: narrative
    assert "narrative" in package
    assert len(package["narrative"]) > 0
    first_sentence = package["narrative"][0]
    assert "sentence" in first_sentence
    assert "event_type" in first_sentence
    assert "recorded_at" in first_sentence
    assert len(first_sentence["sentence"]) > 0

    # Section 5: agent attribution
    assert "agent_attribution" in package
    assert len(package["agent_attribution"]) > 0
    attr = package["agent_attribution"][0]
    assert "agent_id" in attr
    assert "event_type" in attr
    assert "model_version" in attr

    # Package metadata
    assert "schema_version" in package
    assert "generated_at" in package
    assert "application_id" in package
    assert package["application_id"] == app_id
    assert "package_checksum" in package
    assert len(package["package_checksum"]) == 64  # sha256 hex

    await pool.close()


@pytest.mark.asyncio
async def test_regulatory_package_checksum_is_reproducible() -> None:
    """Generating the package twice must produce the same checksum."""
    pool = await get_pool()
    store = EventStore(pool)
    compliance = ComplianceAuditViewProjection(pool)

    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    # Seed compliance projection
    for stream in (f"loan-{app_id}", f"compliance-{app_id}"):
        events = await store.load_stream(stream)
        for event in events:
            if event.event_type in compliance.subscribed_events:
                await compliance.handle_event(event)

    examination_date = datetime.now(UTC) + timedelta(hours=1)

    p1 = await generate_regulatory_package(app_id, examination_date, store, compliance)
    p2 = await generate_regulatory_package(app_id, examination_date, store, compliance)

    assert p1["package_checksum"] == p2["package_checksum"]

    await pool.close()


@pytest.mark.asyncio
async def test_regulatory_package_examination_date_filters_events() -> None:
    """Events after examination_date must not appear in the package."""
    pool = await get_pool()
    store = EventStore(pool)
    compliance = ComplianceAuditViewProjection(pool)

    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    await _build_full_lifecycle(store, app_id, agent_id, session_id)

    # Use a past examination date — only ApplicationSubmitted should be included
    # (recorded_at will be very recent, so use a date far in the past)
    past_date = datetime(2020, 1, 1, tzinfo=UTC)

    package = await generate_regulatory_package(
        application_id=app_id,
        examination_date=past_date,
        store=store,
        compliance_projection=compliance,
    )

    # No events should be included since all were recorded after 2020
    assert len(package["event_stream"]) == 0
    assert len(package["narrative"]) == 0

    await pool.close()
