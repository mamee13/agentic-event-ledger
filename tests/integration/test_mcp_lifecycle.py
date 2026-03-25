"""MCP full-lifecycle integration test.

Drives the complete loan application lifecycle exclusively through MCP tool
calls (mcp.call_tool / mcp.read_resource) — no direct Python function calls.

Requires a running Postgres instance (uses project .env via get_pool()).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from uuid import uuid4

import pytest

from ledger.application.service import LedgerService
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.agent_performance import AgentPerformanceProjection
from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.projections.daemon import ProjectionDaemon
from ledger.infrastructure.store import EventStore
from ledger.mcp import server as mcp_server
from ledger.mcp.server import mcp


@pytest.fixture(autouse=True)
async def _init_server_state() -> AsyncGenerator[None, None]:
    """Initialise the module-level singletons that the MCP tools depend on."""
    pool = await get_pool()
    store = EventStore(pool)

    app_summary = ApplicationSummaryProjection(pool)
    agent_perf = AgentPerformanceProjection(pool)
    compliance = ComplianceAuditViewProjection(pool)
    daemon = ProjectionDaemon(
        store=store,
        projections=[app_summary, agent_perf, compliance],
        pool=pool,
        batch_size=200,
    )

    mcp_server._pool = pool
    mcp_server._store = store
    mcp_server._service = LedgerService(store)
    mcp_server._app_summary = app_summary
    mcp_server._agent_perf = agent_perf
    mcp_server._compliance = compliance
    mcp_server._daemon = daemon

    yield

    await pool.close()


def _parse(result: object) -> dict:  # type: ignore[type-arg]
    """Extract the dict payload from an MCP call_tool result.

    FastMCP.call_tool returns a 2-tuple: ([ContentBlock, ...], raw_dict).
    The raw_dict at index 1 is the already-parsed tool return value.
    """
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    # Fallback: first ContentBlock's .text field
    if isinstance(result, list | tuple) and result:
        blocks = result[0] if isinstance(result[0], list) else list(result)
        for block in blocks:
            text = getattr(block, "text", None)
            if text is not None:
                return json.loads(text)  # type: ignore[no-any-return]
    raise ValueError(f"Unexpected MCP result shape: {type(result)}")


def _parse_resource(result: object) -> dict:  # type: ignore[type-arg]
    """Extract the dict payload from an MCP read_resource result.

    FastMCP.read_resource returns a list of ReadResourceContents with a .content attribute.
    """
    if isinstance(result, dict):
        return result
    if not isinstance(result, list | tuple):
        raise ValueError(f"Unexpected MCP resource result shape: {type(result)}")
    for item in result:
        # ReadResourceContents uses .content (not .text)
        raw = getattr(item, "content", None) or getattr(item, "text", None)
        if raw is not None:
            return json.loads(raw)  # type: ignore[no-any-return]
    raise ValueError(f"Unexpected MCP resource result shape: {type(result)}")


@pytest.mark.asyncio
async def test_full_mcp_lifecycle() -> None:
    """Complete lifecycle driven entirely through MCP tool calls."""
    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    # 1. start_agent_session — MCP tool call
    result = _parse(
        await mcp.call_tool(
            "start_agent_session",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "model_version": "v2.0",
                "context_source": "MLflow Registry",
                "context_token_count": 512,
            },
        )
    )
    assert result["ok"] is True, result
    assert result["stream_id"] == f"agent-{agent_id}-{session_id}"

    # 2. submit_application — MCP tool call
    result = _parse(
        await mcp.call_tool(
            "submit_application",
            {
                "application_id": app_id,
                "applicant_id": "user-test-001",
                "requested_amount_usd": 25000.0,
            },
        )
    )
    assert result["ok"] is True, result
    assert result["stream_id"] == f"loan-{app_id}"

    # 3. duplicate rejected — MCP tool call
    dup = _parse(
        await mcp.call_tool(
            "submit_application",
            {
                "application_id": app_id,
                "applicant_id": "user-test-001",
                "requested_amount_usd": 25000.0,
            },
        )
    )
    assert dup["ok"] is False
    assert dup["error"]["error_type"] == "VALIDATION_ERROR"

    # 4. transition to AWAITING_ANALYSIS — MCP tool call
    result = _parse(await mcp.call_tool("request_credit_analysis", {"application_id": app_id}))
    assert result["ok"] is True, result

    # 5. record_credit_analysis — MCP tool call
    result = _parse(
        await mcp.call_tool(
            "record_credit_analysis",
            {
                "application_id": app_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "risk_tier": "MEDIUM",
                "confidence_score": 0.82,
                "reasoning": "Solid credit history",
                "analysis_duration_ms": 350,
            },
        )
    )
    assert result["ok"] is True, result

    # 6. transition to COMPLIANCE_REVIEW — MCP tool call
    result = _parse(
        await mcp.call_tool(
            "request_compliance_check",
            {"application_id": app_id, "regulation_set_version": "EU-AI-ACT-2024"},
        )
    )
    assert result["ok"] is True, result

    # 7. record compliance checks — MCP tool calls
    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        result = _parse(
            await mcp.call_tool(
                "record_compliance_check",
                {
                    "application_id": app_id,
                    "rule_id": rule,
                    "status": "PASSED",
                    "regulation_set_version": "EU-AI-ACT-2024",
                },
            )
        )
        assert result["ok"] is True, result

    # 8. generate_decision — MCP tool call
    result = _parse(
        await mcp.call_tool(
            "generate_decision",
            {
                "application_id": app_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "recommendation": "APPROVE",
                "confidence_score": 0.82,
                "contributing_sessions": [{"agent_id": agent_id, "session_id": session_id}],
            },
        )
    )
    assert result["ok"] is True, result
    assert result["recommendation"] == "APPROVE"
    assert result["overridden_to_refer"] is False

    # 9. record_human_review — MCP tool call
    result = _parse(
        await mcp.call_tool(
            "record_human_review",
            {
                "application_id": app_id,
                "reviewer_id": "reviewer-jane",
                "final_decision": "APPROVE",
            },
        )
    )
    assert result["ok"] is True, result

    # 10. flush projections
    daemon = mcp_server._daemon
    assert daemon is not None
    await daemon._process_batch()

    # 11. query compliance via MCP resource — no direct Python calls
    resource_result = _parse_resource(
        await mcp.read_resource(f"ledger://applications/{app_id}/compliance")
    )
    assert resource_result.get("status") == "PASSED"
    rules = resource_result.get("rules", {})
    assert isinstance(rules, dict)
    assert rules.get("KYC") == "PASSED"
    assert rules.get("AML") == "PASSED"
    assert rules.get("FRAUD_SCREEN") == "PASSED"


@pytest.mark.asyncio
async def test_confidence_floor_enforced_via_mcp() -> None:
    """confidence_score < 0.6 must be rejected unless recommendation is REFER."""
    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())

    await mcp.call_tool(
        "start_agent_session",
        {"agent_id": agent_id, "session_id": session_id, "model_version": "v2.0"},
    )
    await mcp.call_tool(
        "submit_application",
        {"application_id": app_id, "applicant_id": "user-002", "requested_amount_usd": 5000.0},
    )
    await mcp.call_tool("request_credit_analysis", {"application_id": app_id})
    await mcp.call_tool(
        "record_credit_analysis",
        {
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "risk_tier": "HIGH",
            "confidence_score": 0.45,
            "reasoning": "Risky",
        },
    )
    await mcp.call_tool(
        "request_compliance_check",
        {"application_id": app_id, "regulation_set_version": "EU-AI-ACT-2024"},
    )
    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        await mcp.call_tool(
            "record_compliance_check",
            {
                "application_id": app_id,
                "rule_id": rule,
                "status": "PASSED",
                "regulation_set_version": "EU-AI-ACT-2024",
            },
        )

    # APPROVE with confidence 0.45 — tool converts to REFER before hitting aggregate
    low_conf = _parse(
        await mcp.call_tool(
            "generate_decision",
            {
                "application_id": app_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "recommendation": "APPROVE",
                "confidence_score": 0.45,
                "contributing_sessions": [{"agent_id": agent_id, "session_id": session_id}],
            },
        )
    )
    assert low_conf["ok"] is True, low_conf
    assert low_conf["recommendation"] == "REFER"
    assert low_conf["overridden_to_refer"] is True


@pytest.mark.asyncio
async def test_integrity_check_requires_compliance_role() -> None:
    result = _parse(
        await mcp.call_tool(
            "run_integrity_check",
            {
                "entity_type": "loan",
                "entity_id": str(uuid4()),
                "compliance_role": "ANALYST",
            },
        )
    )
    assert result["ok"] is False
    assert result["error"]["error_type"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_validation_errors_are_structured() -> None:
    """All validation errors must carry the required 6 fields."""
    result = _parse(
        await mcp.call_tool(
            "submit_application",
            {"application_id": "", "applicant_id": "x", "requested_amount_usd": 100.0},
        )
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
async def test_causal_chain_rejects_unrelated_session() -> None:
    """Rule 6: generate_decision must reject a contributing session that never
    processed the loan — driven entirely through MCP tool calls."""
    app_id = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    session_id = str(uuid4())
    unrelated_agent_id = f"agent-{uuid4()}"
    unrelated_session_id = str(uuid4())

    await mcp.call_tool(
        "start_agent_session",
        {"agent_id": agent_id, "session_id": session_id, "model_version": "v2.0"},
    )
    await mcp.call_tool(
        "start_agent_session",
        {
            "agent_id": unrelated_agent_id,
            "session_id": unrelated_session_id,
            "model_version": "v2.0",
        },
    )
    await mcp.call_tool(
        "submit_application",
        {
            "application_id": app_id,
            "applicant_id": "user-causal-001",
            "requested_amount_usd": 10000.0,
        },
    )
    await mcp.call_tool("request_credit_analysis", {"application_id": app_id})
    await mcp.call_tool(
        "record_credit_analysis",
        {
            "application_id": app_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "risk_tier": "LOW",
            "confidence_score": 0.9,
            "reasoning": "Clean history",
        },
    )
    await mcp.call_tool("request_compliance_check", {"application_id": app_id})
    for rule in ("KYC", "AML", "FRAUD_SCREEN", "DATA_PRIVACY", "MODEL_BIAS"):
        await mcp.call_tool(
            "record_compliance_check",
            {"application_id": app_id, "rule_id": rule, "status": "PASSED"},
        )

    # Unrelated session — must be rejected
    result = _parse(
        await mcp.call_tool(
            "generate_decision",
            {
                "application_id": app_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "recommendation": "APPROVE",
                "confidence_score": 0.9,
                "contributing_sessions": [
                    {"agent_id": unrelated_agent_id, "session_id": unrelated_session_id}
                ],
            },
        )
    )
    assert result["ok"] is False, f"Expected causal chain rejection, got: {result}"
    assert result["error"]["error_type"] == "DOMAIN_RULE_VIOLATION"
    assert "Causal Chain" in result["error"]["message"]

    # Correct session — must succeed
    result = _parse(
        await mcp.call_tool(
            "generate_decision",
            {
                "application_id": app_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "recommendation": "APPROVE",
                "confidence_score": 0.9,
                "contributing_sessions": [{"agent_id": agent_id, "session_id": session_id}],
            },
        )
    )
    assert result["ok"] is True, f"Expected success with correct session, got: {result}"
    assert result["recommendation"] == "APPROVE"
