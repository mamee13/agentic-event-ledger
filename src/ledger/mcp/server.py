"""MCP Server for The Ledger — Phase 6.

8 tools + projection-backed resources + health endpoint.

All tool errors are structured dicts:
  { error_type, message, stream_id, expected_version, actual_version, suggested_action }
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from mcp.server.fastmcp import FastMCP

from ledger.application.service import LedgerService
from ledger.core.agent_context import reconstruct_agent_context
from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.agent_performance import AgentPerformanceProjection
from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.projections.daemon import ProjectionDaemon
from ledger.infrastructure.store import EventStore
from ledger.mcp.errors import (
    from_exception,
    not_found_error,
    rate_limit_error,
    validation_error,
)
from ledger.mcp.rate_limit import integrity_check_limiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state — initialised in lifespan, used by all tools
# ---------------------------------------------------------------------------

_pool: asyncpg.Pool | None = None
_store: EventStore | None = None
_service: LedgerService | None = None
_app_summary: ApplicationSummaryProjection | None = None
_agent_perf: AgentPerformanceProjection | None = None
_compliance: ComplianceAuditViewProjection | None = None
_daemon: ProjectionDaemon | None = None
_daemon_task: asyncio.Task[None] | None = None


def _get_pool() -> asyncpg.Pool:
    assert _pool is not None, "Pool not initialised"
    return _pool


def _get_store() -> EventStore:
    assert _store is not None, "EventStore not initialised"
    return _store


def _get_service() -> LedgerService:
    assert _service is not None, "LedgerService not initialised"
    return _service


def _get_compliance() -> ComplianceAuditViewProjection:
    assert _compliance is not None, "ComplianceAuditViewProjection not initialised"
    return _compliance


def _get_daemon() -> ProjectionDaemon:
    assert _daemon is not None, "ProjectionDaemon not initialised"
    return _daemon


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    global _pool, _store, _service, _app_summary, _agent_perf, _compliance
    global _daemon, _daemon_task

    _pool = await get_pool()
    _store = EventStore(_pool)
    _service = LedgerService(_store)
    _app_summary = ApplicationSummaryProjection(_pool)
    _agent_perf = AgentPerformanceProjection(_pool)
    _compliance = ComplianceAuditViewProjection(_pool)
    _daemon = ProjectionDaemon(
        store=_store,
        projections=[_app_summary, _agent_perf, _compliance],
        pool=_pool,
        batch_size=200,
    )
    _daemon_task = asyncio.create_task(_daemon.run_forever(poll_interval_ms=100))
    logger.info("Ledger MCP server started")

    try:
        yield
    finally:
        if _daemon:
            _daemon.stop()
        if _daemon_task:
            _daemon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(_daemon_task, timeout=3.0)
        if _pool:
            await _pool.close()
        logger.info("Ledger MCP server stopped")


# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP("ledger", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(**kwargs: Any) -> dict[str, Any]:
    return {"ok": True, **kwargs}


def _err(error: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "error": error}


# Active regulation sets — rule_id must exist here for record_compliance_check
_REGULATION_SETS: dict[str, set[str]] = {
    "EU-AI-ACT-2025": {"KYC", "AML", "FRAUD_SCREEN", "DATA_PRIVACY", "MODEL_BIAS"},
    "EU-AI-ACT-2024": {"KYC", "AML", "FRAUD_SCREEN"},
}
_DEFAULT_REG_SET = "EU-AI-ACT-2025"

# ---------------------------------------------------------------------------
# Tool 1 — submit_application
# ---------------------------------------------------------------------------


@mcp.tool()
async def submit_application(
    application_id: str,
    applicant_id: str,
    requested_amount_usd: float,
) -> dict[str, Any]:
    """Submit a new loan application. Validates schema and rejects duplicates."""
    if not application_id.strip():
        return _err(validation_error("application_id must not be empty."))
    if not applicant_id.strip():
        return _err(validation_error("applicant_id must not be empty."))
    if requested_amount_usd <= 0:
        return _err(validation_error("requested_amount_usd must be positive."))

    stream_id = f"loan-{application_id}"
    if await _get_store().stream_version(stream_id) != -1:
        return _err(
            validation_error(
                f"Application {application_id} already exists.",
                suggested_action="Use a unique application_id.",
            )
        )

    try:
        await _get_service().submit_application(
            loan_id=application_id,
            amount=requested_amount_usd,
            applicant_id=applicant_id,
        )
    except Exception as exc:
        return _err(from_exception(exc, stream_id))

    return _ok(application_id=application_id, stream_id=stream_id)


# ---------------------------------------------------------------------------
# Tool 2 — start_agent_session
# ---------------------------------------------------------------------------


@mcp.tool()
async def start_agent_session(
    agent_id: str,
    session_id: str,
    model_version: str,
    context_source: str = "MLflow Registry",
    context_token_count: int = 1024,
) -> dict[str, Any]:
    """Start a new agent session. Writes AgentContextLoaded as the first event."""
    if not agent_id.strip():
        return _err(validation_error("agent_id must not be empty."))
    if not session_id.strip():
        return _err(validation_error("session_id must not be empty."))
    if not model_version.strip():
        return _err(validation_error("model_version must not be empty."))
    if context_token_count < 0:
        return _err(validation_error("context_token_count must be non-negative."))

    stream_id = f"agent-{agent_id}-{session_id}"
    if await _get_store().stream_version(stream_id) != -1:
        return _err(
            validation_error(
                f"Session {session_id} for agent {agent_id} already exists.",
                suggested_action="Use a unique session_id.",
            )
        )

    try:
        event = BaseEvent(
            event_type="AgentContextLoaded",
            payload={
                "agent_id": agent_id,
                "session_id": session_id,
                "context_source": context_source,
                "context_token_count": context_token_count,
                "model_version": model_version,
                "event_replay_from_position": 0,
            },
        )
        await _get_store().append(stream_id, [event], expected_version=-1)
    except Exception as exc:
        return _err(from_exception(exc, stream_id))

    return _ok(agent_id=agent_id, session_id=session_id, stream_id=stream_id)


# ---------------------------------------------------------------------------
# Tool 3 — record_credit_analysis
# ---------------------------------------------------------------------------


@mcp.tool()
async def record_credit_analysis(
    application_id: str,
    agent_id: str,
    session_id: str,
    risk_tier: str,
    confidence_score: float,
    reasoning: str = "",
    analysis_duration_ms: int = 0,
) -> dict[str, Any]:
    """Record a completed credit analysis.

    Requires an active AgentSession with AgentContextLoaded as first event.
    Enforces optimistic concurrency on the loan stream.
    """
    loan_stream = f"loan-{application_id}"
    session_stream = f"agent-{agent_id}-{session_id}"

    if risk_tier not in {"LOW", "MEDIUM", "HIGH", "VERY_HIGH"}:
        return _err(
            validation_error(
                f"Invalid risk_tier '{risk_tier}'. Must be LOW, MEDIUM, HIGH, or VERY_HIGH."
            )
        )
    if not (0.0 <= confidence_score <= 1.0):
        return _err(validation_error("confidence_score must be between 0.0 and 1.0."))

    # Verify session exists and context is loaded
    if await _get_store().stream_version(session_stream) == -1:
        return _err(not_found_error(f"AgentSession {session_stream}"))
    session_events = await _get_store().load_stream(session_stream)
    if not session_events or session_events[0].event_type != "AgentContextLoaded":
        return _err(
            validation_error(
                "AgentSession has no AgentContextLoaded event.",
                suggested_action="Call start_agent_session first.",
            )
        )

    try:
        await _get_service().record_credit_analysis(
            loan_id=application_id,
            agent_id=agent_id,
            session_id=session_id,
            risk_score=confidence_score,
            reasoning=reasoning,
            analysis_duration_ms=analysis_duration_ms,
            risk_tier=risk_tier,
            confidence_score=confidence_score,
        )
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(
        application_id=application_id,
        agent_id=agent_id,
        session_id=session_id,
        risk_tier=risk_tier,
    )


# ---------------------------------------------------------------------------
# Tool 4 — record_fraud_screening
# ---------------------------------------------------------------------------


@mcp.tool()
async def record_fraud_screening(
    application_id: str,
    agent_id: str,
    session_id: str,
    fraud_score: float,
    screening_result: str = "PASS",
    flags: list[str] | None = None,
) -> dict[str, Any]:
    """Record a completed fraud screening.

    Requires an active AgentSession with context loaded.
    fraud_score must be in 0.0–1.0.
    """
    loan_stream = f"loan-{application_id}"
    session_stream = f"agent-{agent_id}-{session_id}"

    if not (0.0 <= fraud_score <= 1.0):
        return _err(validation_error("fraud_score must be between 0.0 and 1.0."))
    if screening_result not in {"PASS", "FAIL", "REVIEW"}:
        return _err(validation_error("screening_result must be PASS, FAIL, or REVIEW."))

    # Verify session exists and context is loaded
    if await _get_store().stream_version(session_stream) == -1:
        return _err(not_found_error(f"AgentSession {session_stream}"))
    session_events = await _get_store().load_stream(session_stream)
    if not session_events or session_events[0].event_type != "AgentContextLoaded":
        return _err(
            validation_error(
                "AgentSession has no AgentContextLoaded event.",
                suggested_action="Call start_agent_session first.",
            )
        )

    # Verify loan exists
    if await _get_store().stream_version(loan_stream) == -1:
        return _err(not_found_error(f"Loan application {loan_stream}"))

    try:
        event = BaseEvent(
            event_type="FraudScreeningCompleted",
            payload={
                "application_id": application_id,
                "agent_id": agent_id,
                "session_id": session_id,
                "fraud_score": fraud_score,
                "screening_result": screening_result,
                "flags": flags or [],
            },
        )
        # Append to session stream first (tracks contribution)
        s_ver = await _get_store().stream_version(session_stream)
        await _get_store().append(session_stream, [event], expected_version=s_ver)
        # Append to loan stream
        l_ver = await _get_store().stream_version(loan_stream)
        await _get_store().append(loan_stream, [event], expected_version=l_ver)
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(
        application_id=application_id,
        fraud_score=fraud_score,
        screening_result=screening_result,
    )


# ---------------------------------------------------------------------------
# Tool 5 — record_compliance_check
# ---------------------------------------------------------------------------


@mcp.tool()
async def record_compliance_check(
    application_id: str,
    rule_id: str,
    status: str,
    regulation_set_version: str = _DEFAULT_REG_SET,
) -> dict[str, Any]:
    """Record a compliance rule result.

    rule_id must exist in the active regulation_set_version.
    status must be PASSED or FAILED.
    """
    loan_stream = f"loan-{application_id}"

    if status not in {"PASSED", "FAILED"}:
        return _err(validation_error("status must be PASSED or FAILED."))

    valid_rules = _REGULATION_SETS.get(regulation_set_version)
    if valid_rules is None:
        return _err(
            validation_error(
                f"Unknown regulation_set_version '{regulation_set_version}'.",
                suggested_action=f"Use one of: {sorted(_REGULATION_SETS)}",
            )
        )
    if rule_id not in valid_rules:
        return _err(
            validation_error(
                f"rule_id '{rule_id}' not in regulation set '{regulation_set_version}'.",
                suggested_action=f"Valid rules: {sorted(valid_rules)}",
            )
        )

    if await _get_store().stream_version(loan_stream) == -1:
        return _err(not_found_error(f"Loan application {loan_stream}"))

    try:
        await _get_service().record_compliance(
            loan_id=application_id,
            rule_id=rule_id,
            status=status,
        )
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(
        application_id=application_id,
        rule_id=rule_id,
        status=status,
        regulation_set_version=regulation_set_version,
    )


# ---------------------------------------------------------------------------
# Tool 6 — generate_decision
# ---------------------------------------------------------------------------


@mcp.tool()
async def generate_decision(
    application_id: str,
    agent_id: str,
    session_id: str,
    recommendation: str,
    confidence_score: float,
    contributing_sessions: list[dict[str, str]],
) -> dict[str, Any]:
    """Generate a decision for a loan application.

    Enforces confidence floor (< 0.6 → REFER) and causal chain validation.
    """
    loan_stream = f"loan-{application_id}"

    if recommendation not in {"APPROVE", "DECLINE", "REFER"}:
        return _err(validation_error("recommendation must be APPROVE, DECLINE, or REFER."))
    if not (0.0 <= confidence_score <= 1.0):
        return _err(validation_error("confidence_score must be between 0.0 and 1.0."))
    if not contributing_sessions:
        return _err(
            validation_error(
                "contributing_sessions must not be empty.",
                suggested_action="Include at least one contributing agent session.",
            )
        )
    for cs in contributing_sessions:
        if "agent_id" not in cs or "session_id" not in cs:
            return _err(
                validation_error(
                    "Each contributing_session must have 'agent_id' and 'session_id' keys."
                )
            )

    # Enforce confidence floor before hitting the aggregate
    effective = "REFER" if confidence_score < 0.6 else recommendation

    try:
        await _get_service().generate_decision(
            loan_id=application_id,
            agent_id=agent_id,
            session_id=session_id,
            recommendation=effective,
            confidence=confidence_score,
            contributing_sessions=contributing_sessions,
        )
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(
        application_id=application_id,
        recommendation=effective,
        confidence_score=confidence_score,
        overridden_to_refer=effective != recommendation,
    )


# ---------------------------------------------------------------------------
# Tool 7 — record_human_review
# ---------------------------------------------------------------------------


@mcp.tool()
async def record_human_review(
    application_id: str,
    reviewer_id: str,
    final_decision: str,
    override: bool = False,
    override_reason: str = "",
) -> dict[str, Any]:
    """Record a human reviewer's decision.

    reviewer_id is required. If override=True, override_reason must be provided.
    """
    loan_stream = f"loan-{application_id}"

    if not reviewer_id.strip():
        return _err(
            validation_error(
                "reviewer_id is required.",
                suggested_action="Provide an authenticated reviewer_id.",
            )
        )
    if final_decision not in {"APPROVE", "DECLINE", "REFER"}:
        return _err(validation_error("final_decision must be APPROVE, DECLINE, or REFER."))
    if override and not override_reason.strip():
        return _err(
            validation_error(
                "override_reason is required when override=True.",
                suggested_action="Provide a reason for the override.",
            )
        )

    try:
        await _get_service().record_human_review(
            loan_id=application_id,
            reviewer_id=reviewer_id,
            decision=final_decision,
            override=override,
            override_reason=override_reason if override else None,
        )
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(
        application_id=application_id,
        reviewer_id=reviewer_id,
        final_decision=final_decision,
        override=override,
    )


# ---------------------------------------------------------------------------
# Tool 8 — run_integrity_check
# ---------------------------------------------------------------------------


@mcp.tool()
async def run_integrity_check(
    entity_type: str,
    entity_id: str,
    compliance_role: str,
) -> dict[str, Any]:
    """Run a cryptographic integrity check on an audit stream.

    Requires compliance_role = 'COMPLIANCE_OFFICER'.
    Rate-limited to 1 call per minute per entity.
    """
    if compliance_role != "COMPLIANCE_OFFICER":
        return _err(
            validation_error(
                "Only COMPLIANCE_OFFICER role may run integrity checks.",
                suggested_action="Authenticate with the COMPLIANCE_OFFICER role.",
            )
        )

    rate_key = f"{entity_type}:{entity_id}"
    if not integrity_check_limiter.is_allowed(rate_key):
        wait = integrity_check_limiter.seconds_until_allowed(rate_key)
        return _err(
            rate_limit_error(
                f"Rate limit exceeded for {entity_type}/{entity_id}. Retry in {wait:.0f}s."
            )
        )

    audit_stream = f"audit-{entity_type}-{entity_id}"
    try:
        new_hash, previous_hash = await _get_service().run_audit_integrity_check(loan_id=entity_id)
    except Exception as exc:
        return _err(from_exception(exc, audit_stream))

    return _ok(
        audit_stream=audit_stream,
        integrity_hash=new_hash,
        previous_hash=previous_hash,
    )


# ---------------------------------------------------------------------------
# Resources — projection-backed (no aggregate replay) unless noted
# ---------------------------------------------------------------------------


@mcp.resource("ledger://applications/{application_id}")
async def get_application(application_id: str) -> str:
    """Application summary from projection."""
    row = await _get_pool().fetchrow(
        "SELECT * FROM projection_application_summary WHERE application_id = $1",
        application_id,
    )
    if not row:
        return json.dumps(not_found_error(f"Application {application_id}"))
    return json.dumps(dict(row), default=str)


@mcp.resource("ledger://applications/{application_id}/audit-trail")
async def get_audit_trail(application_id: str) -> str:
    """Full audit trail — reads AuditLedger stream directly (not a projection)."""
    stream_id = f"audit-loan-{application_id}"
    try:
        events = await _get_store().load_stream(stream_id)
    except Exception as exc:
        return json.dumps(from_exception(exc, stream_id))
    return json.dumps(
        [
            {
                "event_type": e.event_type,
                "stream_position": e.stream_position,
                "global_position": e.global_position,
                "recorded_at": e.recorded_at.isoformat(),
                "payload": e.payload,
            }
            for e in events
        ]
    )


@mcp.resource("ledger://agents/{agent_id}/sessions/{session_id}")
async def get_agent_session(agent_id: str, session_id: str) -> str:
    """Agent session context — reads AgentSession stream directly (not a projection)."""
    stream_id = f"agent-{agent_id}-{session_id}"
    try:
        ctx = await reconstruct_agent_context(stream_id, _get_store())
    except Exception as exc:
        return json.dumps(from_exception(exc, stream_id))
    return json.dumps(
        {
            "session_id": ctx.session_id,
            "agent_id": ctx.agent_id,
            "model_version": ctx.model_version,
            "is_active": ctx.is_active,
            "last_completed_action": ctx.last_completed_action,
            "pending_work": ctx.pending_work,
            "needs_reconciliation": ctx.needs_reconciliation,
            "total_events": ctx.total_events,
            "summary": ctx.summary,
            "recent_events": ctx.recent_events,
        }
    )


@mcp.resource("ledger://applications/{application_id}/compliance")
async def get_compliance(application_id: str) -> str:
    """Current compliance state from projection."""
    try:
        state = await _get_compliance().get_current_compliance(application_id)
    except Exception as exc:
        return json.dumps(from_exception(exc))
    return json.dumps(state, default=str)


@mcp.resource("ledger://agents/{agent_id}/performance")
async def get_agent_performance(agent_id: str) -> str:
    """Agent performance metrics from projection."""
    rows = await _get_pool().fetch(
        "SELECT * FROM projection_agent_performance WHERE agent_id = $1",
        agent_id,
    )
    if not rows:
        return json.dumps(not_found_error(f"Agent {agent_id}"))
    return json.dumps([dict(r) for r in rows], default=str)


@mcp.resource("ledger://ledger/health")
async def get_health() -> str:
    """Health check — returns get_lag() for every projection. Target: <10ms per call."""
    daemon = _get_daemon()
    lags: dict[str, int] = {}
    for name in ("ApplicationSummary", "AgentPerformanceLedger", "ComplianceAuditView"):
        try:
            lags[name] = await daemon.get_lag(name)
        except Exception:
            lags[name] = -1
    return json.dumps({"status": "ok", "projection_lags": lags})
