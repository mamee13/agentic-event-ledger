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
    generate_regulatory_package_tool,
    record_compliance_check,
    record_credit_analysis,
    record_human_review,
    request_compliance_check,
    request_credit_analysis,
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
    # Transition loan to AWAITING_ANALYSIS via tool
    result = await request_credit_analysis(application_id=app_id)
    assert result["ok"] is True, result
    assert result["state"] == "AWAITING_ANALYSIS"

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
    # Transition loan to COMPLIANCE_REVIEW via tool
    result = await request_compliance_check(
        application_id=app_id, regulation_set_version="EU-AI-ACT-2024"
    )
    assert result["ok"] is True, result
    assert result["state"] == "COMPLIANCE_REVIEW"

    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        result = await record_compliance_check(
            application_id=app_id,
            rule_id=rule,
            status="PASSED",
            regulation_set_version="EU-AI-ACT-2024",
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
    # Drive through analysis + compliance for app2 via tools
    await request_credit_analysis(application_id=app_id2)
    await record_credit_analysis(
        application_id=app_id2,
        agent_id=agent_id2,
        session_id=session_id2,
        risk_tier="HIGH",
        confidence_score=0.45,  # below floor
        reasoning="Risky",
    )
    await request_compliance_check(application_id=app_id2, regulation_set_version="EU-AI-ACT-2024")
    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        await record_compliance_check(
            application_id=app_id2,
            rule_id=rule,
            status="PASSED",
            regulation_set_version="EU-AI-ACT-2024",
        )
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


@pytest.mark.asyncio
async def test_regulatory_package_requires_compliance_role() -> None:
    result = await generate_regulatory_package_tool(
        application_id=str(uuid4()),
        examination_date="2026-03-21T00:00:00",
        compliance_role="ANALYST",
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_regulatory_package_rejects_bad_date() -> None:
    result = await generate_regulatory_package_tool(
        application_id=str(uuid4()),
        examination_date="not-a-date",
        compliance_role="COMPLIANCE_OFFICER",
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_regulatory_package_rejects_missing_application() -> None:
    result = await generate_regulatory_package_tool(
        application_id="does-not-exist",
        examination_date="2026-03-21T00:00:00",
        compliance_role="COMPLIANCE_OFFICER",
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_regulatory_package_full() -> None:
    """Generate a package for a submitted application and verify structure."""
    from datetime import UTC, datetime

    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    # Minimal lifecycle: session + application only
    await start_agent_session(agent_id=agent_id, session_id=session_id, model_version="v2.0")
    await submit_application(
        application_id=app_id,
        applicant_id="user-reg-001",
        requested_amount_usd=10000.0,
    )

    result = await generate_regulatory_package_tool(
        application_id=app_id,
        examination_date=datetime.now(UTC).isoformat(),
        compliance_role="COMPLIANCE_OFFICER",
    )
    assert result["ok"] is True, result

    pkg = result["package"]
    # Required top-level keys per spec
    for key in (
        "schema_version",
        "generated_at",
        "application_id",
        "examination_date",
        "package_checksum",
        "event_stream",
        "projection_states",
        "integrity_verification",
        "narrative",
        "agent_attribution",
    ):
        assert key in pkg, f"Missing key: {key}"

    assert pkg["application_id"] == app_id
    assert len(pkg["event_stream"]) >= 1
    assert pkg["package_checksum"] != ""
    assert pkg["integrity_verification"]["is_valid"] is True


@pytest.mark.asyncio
async def test_causal_chain_rejects_unrelated_session() -> None:
    """Rule 6: generate_decision must reject a contributing session that never
    processed the loan. The service loads each AgentSessionAggregate and calls
    validate_causal_chain — this test proves the wiring is end-to-end, not just
    at the aggregate unit level."""
    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    # Unrelated session — started but never processed this loan
    unrelated_agent_id = f"agent-{uuid4()}"
    unrelated_session_id = str(uuid4())

    # Start both sessions
    await start_agent_session(agent_id=agent_id, session_id=session_id, model_version="v2.0")
    await start_agent_session(
        agent_id=unrelated_agent_id, session_id=unrelated_session_id, model_version="v2.0"
    )

    # Drive the loan through to PENDING_DECISION using the real session
    await submit_application(
        application_id=app_id, applicant_id="user-causal-001", requested_amount_usd=10000.0
    )
    await request_credit_analysis(application_id=app_id)
    await record_credit_analysis(
        application_id=app_id,
        agent_id=agent_id,
        session_id=session_id,
        risk_tier="LOW",
        confidence_score=0.9,
        reasoning="Clean history",
    )
    await request_compliance_check(application_id=app_id)
    for rule in ("KYC", "AML", "FRAUD_SCREEN", "DATA_PRIVACY", "MODEL_BIAS"):
        await record_compliance_check(application_id=app_id, rule_id=rule, status="PASSED")

    # Attempt to generate a decision citing the UNRELATED session — must be rejected
    result = await generate_decision(
        application_id=app_id,
        agent_id=agent_id,
        session_id=session_id,
        recommendation="APPROVE",
        confidence_score=0.9,
        contributing_sessions=[
            {"agent_id": unrelated_agent_id, "session_id": unrelated_session_id}
        ],
    )
    assert result["ok"] is False, f"Expected causal chain rejection, got: {result}"
    assert result["error"]["error_type"] == "DOMAIN_RULE_VIOLATION"
    assert "Causal Chain" in result["error"]["message"]

    # Now generate with the CORRECT session — must succeed
    result = await generate_decision(
        application_id=app_id,
        agent_id=agent_id,
        session_id=session_id,
        recommendation="APPROVE",
        confidence_score=0.9,
        contributing_sessions=[{"agent_id": agent_id, "session_id": session_id}],
    )
    assert result["ok"] is True, f"Expected success with correct session, got: {result}"
    assert result["recommendation"] == "APPROVE"
