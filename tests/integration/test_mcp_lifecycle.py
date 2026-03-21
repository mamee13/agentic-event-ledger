"""MCP full-lifecycle integration test.

Runs the complete loan application lifecycle using only MCP tool functions:
  start_agent_session
  → submit_application
  → record_credit_analysis
  → record_compliance_check (all required rules)
  → generate_decision
  → record_human_review
  → query compliance view via resource helper

Requires a running Postgres instance (uses project .env via get_pool()).
"""

from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.store import EventStore

# Import the tool functions directly — they are plain async functions
from ledger.mcp import server as mcp_server
from ledger.mcp.server import (
    generate_decision,
    record_compliance_check,
    record_credit_analysis,
    record_human_review,
    run_integrity_check,
    start_agent_session,
    submit_application,
)


@pytest.fixture(autouse=True)
async def _init_server_state() -> AsyncGenerator[None, None]:
    """Initialise the module-level singletons that the tool functions depend on."""
    pool = await get_pool()
    store = EventStore(pool)

    from ledger.application.service import LedgerService
    from ledger.infrastructure.projections.agent_performance import AgentPerformanceProjection
    from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
    from ledger.infrastructure.projections.daemon import ProjectionDaemon

    app_summary = ApplicationSummaryProjection(pool)
    agent_perf = AgentPerformanceProjection(pool)
    compliance = ComplianceAuditViewProjection(pool)
    daemon = ProjectionDaemon(
        store=store,
        projections=[app_summary, agent_perf, compliance],
        pool=pool,
        batch_size=200,
    )

    # Inject into module globals so tool functions resolve correctly
    mcp_server._pool = pool
    mcp_server._store = store
    mcp_server._service = LedgerService(store)
    mcp_server._app_summary = app_summary
    mcp_server._agent_perf = agent_perf
    mcp_server._compliance = compliance
    mcp_server._daemon = daemon

    yield

    await pool.close()


@pytest.mark.asyncio
async def test_full_mcp_lifecycle() -> None:
    """Complete lifecycle: session → application → analysis → compliance → decision → review."""
    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    # ------------------------------------------------------------------ 1. start session
    result = await start_agent_session(
        agent_id=agent_id,
        session_id=session_id,
        model_version="v2.0",
        context_source="MLflow Registry",
        context_token_count=512,
    )
    assert result["ok"] is True, result
    assert result["stream_id"] == f"agent-{agent_id}-{session_id}"

    # ------------------------------------------------------------------ 2. submit application
    result = await submit_application(
        application_id=app_id,
        applicant_id="user-test-001",
        requested_amount_usd=25000.0,
    )
    assert result["ok"] is True, result
    assert result["stream_id"] == f"loan-{app_id}"

    # ------------------------------------------------------------------ 3. duplicate rejected
    dup = await submit_application(
        application_id=app_id,
        applicant_id="user-test-001",
        requested_amount_usd=25000.0,
    )
    assert dup["ok"] is False
    assert dup["error"]["error_type"] == "VALIDATION_ERROR"

    # ------------------------------------------------------------------ 4. record credit analysis
    # First need to transition loan to AWAITING_ANALYSIS via service directly
    # (the MCP tool record_credit_analysis expects the loan to be in AWAITING_ANALYSIS)
    store = mcp_server._store
    assert store is not None
    from ledger.core.models import BaseEvent

    loan_ver = await store.stream_version(f"loan-{app_id}")
    await store.append(
        f"loan-{app_id}",
        [BaseEvent(event_type="CreditAnalysisRequested", payload={"application_id": app_id})],
        expected_version=loan_ver,
    )

    result = await record_credit_analysis(
        application_id=app_id,
        agent_id=agent_id,
        session_id=session_id,
        risk_tier="MEDIUM",
        confidence_score=0.82,
        reasoning="Solid credit history",
        analysis_duration_ms=350,
    )
    assert result["ok"] is True, result

    # ------------------------------------------------------------------ 5. compliance checks
    # Transition loan to COMPLIANCE_REVIEW
    service = mcp_server._service
    assert service is not None
    await service.request_compliance_check(app_id)

    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        result = await record_compliance_check(
            application_id=app_id,
            rule_id=rule,
            status="PASSED",
            regulation_set_version="EU-AI-ACT-2025",
        )
        assert result["ok"] is True, result

    # ------------------------------------------------------------------ 6. generate decision
    # After all 3 rules pass, loan is in PENDING_DECISION
    result = await generate_decision(
        application_id=app_id,
        agent_id=agent_id,
        session_id=session_id,
        recommendation="APPROVE",
        confidence_score=0.82,
        contributing_sessions=[{"agent_id": agent_id, "session_id": session_id}],
    )
    assert result["ok"] is True, result
    assert result["recommendation"] == "APPROVE"
    assert result["overridden_to_refer"] is False

    # ------------------------------------------------------------------ 7. confidence floor
    app_id2 = str(uuid4())
    agent_id2 = f"agent-{uuid4()}"
    session_id2 = str(uuid4())

    await start_agent_session(agent_id=agent_id2, session_id=session_id2, model_version="v2.0")
    await submit_application(
        application_id=app_id2, applicant_id="user-002", requested_amount_usd=5000.0
    )
    # Drive through analysis + compliance for app2
    store2 = mcp_server._store
    assert store2 is not None
    v = await store2.stream_version(f"loan-{app_id2}")
    await store2.append(
        f"loan-{app_id2}",
        [BaseEvent(event_type="CreditAnalysisRequested", payload={"application_id": app_id2})],
        expected_version=v,
    )
    await record_credit_analysis(
        application_id=app_id2,
        agent_id=agent_id2,
        session_id=session_id2,
        risk_tier="HIGH",
        confidence_score=0.45,  # below floor
        reasoning="Risky",
    )
    assert service is not None
    await service.request_compliance_check(app_id2)
    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        await record_compliance_check(application_id=app_id2, rule_id=rule, status="PASSED")
    low_conf = await generate_decision(
        application_id=app_id2,
        agent_id=agent_id2,
        session_id=session_id2,
        recommendation="APPROVE",
        confidence_score=0.45,
        contributing_sessions=[{"agent_id": agent_id2, "session_id": session_id2}],
    )
    assert low_conf["ok"] is True, low_conf
    assert low_conf["recommendation"] == "REFER"
    assert low_conf["overridden_to_refer"] is True

    # ------------------------------------------------------------------ 8. record human review
    result = await record_human_review(
        application_id=app_id,
        reviewer_id="reviewer-jane",
        final_decision="APPROVE",
    )
    assert result["ok"] is True, result

    # override=True without reason must fail
    bad = await record_human_review(
        application_id=app_id,
        reviewer_id="reviewer-jane",
        final_decision="APPROVE",
        override=True,
        override_reason="",
    )
    assert bad["ok"] is False
    assert bad["error"]["error_type"] == "VALIDATION_ERROR"

    # Ensure projections are updated
    daemon = mcp_server._daemon
    assert daemon is not None
    await daemon._process_batch()

    # ------------------------------------------------------------------ 9. query compliance view
    compliance_proj = mcp_server._compliance
    assert compliance_proj is not None
    state = await compliance_proj.get_current_compliance(app_id)
    assert state["status"] == "PASSED"
    rules = state["rules"]
    assert isinstance(rules, dict)
    assert rules.get("KYC") == "PASSED"
    assert rules.get("AML") == "PASSED"
    assert rules.get("FRAUD_SCREEN") == "PASSED"


@pytest.mark.asyncio
async def test_integrity_check_requires_compliance_role() -> None:
    result = await run_integrity_check(
        entity_type="loan",
        entity_id=str(uuid4()),
        compliance_role="ANALYST",
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_integrity_check_rate_limited() -> None:
    from ledger.mcp.rate_limit import RateLimiter

    # Use a fresh limiter with 60-second window so second call is always blocked
    limiter = RateLimiter(calls=1, period_seconds=60.0)
    entity_id = str(uuid4())
    key = f"loan:{entity_id}"
    assert limiter.is_allowed(key) is True
    assert limiter.is_allowed(key) is False


@pytest.mark.asyncio
async def test_validation_errors_are_structured() -> None:
    """All validation errors must carry the required 5 fields."""
    result = await submit_application(
        application_id="",
        applicant_id="x",
        requested_amount_usd=100.0,
    )
    assert result["ok"] is False
    err = result["error"]
    for field in (
        "error_type",
        "message",
        "stream_id",
        "expected_version",
        "actual_version",
        "suggested_action",
    ):
        assert field in err, f"Missing field: {field}"
