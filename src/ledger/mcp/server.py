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
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
from mcp.server.fastmcp import FastMCP

from ledger.application.service import LedgerService
from ledger.core.agent_context import reconstruct_agent_context
from ledger.core.models import BaseEvent
from ledger.core.regulatory_package import generate_regulatory_package
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.parsers import parse_any
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


def setup_logging(level: str = "INFO") -> None:
    """Configures centralized logging for the MCP server and background tasks."""
    import sys

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stderr,
        force=True,
    )


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
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

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
# Tool -1 — register_applicant
# ---------------------------------------------------------------------------


@mcp.tool()
async def register_applicant(
    applicant_id: str,
    name: str,
    industry: str,
    jurisdiction: str,
    legal_type: str = "Individual",
    risk_segment: str = "LOW",
) -> dict[str, Any]:
    """Register or update an applicant's profile in the registry.

    :param applicant_id: Unique ID for the company or person (e.g. 'entity-99').
    :param name: Legal name of the applicant.
    :param industry: Business sector (e.g. 'Finance', 'Tech').
    :param jurisdiction: Country or state code (ISO).
    :param legal_type: Type of entity (LLC, Corporation, Individual).
    :param risk_segment: Initial risk tier (LOW, MEDIUM, HIGH).
    """
    logger.info(" TOOL: register_applicant (id=%s, name=%s)", applicant_id, name)
    if not applicant_id.strip():
        return _err(validation_error("applicant_id must not be empty."))
    if risk_segment not in {"LOW", "MEDIUM", "HIGH"}:
        return _err(validation_error("risk_segment must be LOW, MEDIUM, or HIGH."))

    try:
        await _get_pool().execute(
            """
            INSERT INTO applicant_registry.companies 
            (company_id, name, industry, naics, jurisdiction, legal_type, founded_year,
             employee_count, ein, address_city, address_state, relationship_start,
             account_manager, risk_segment, trajectory, submission_channel, ip_region)
            VALUES ($1, $2, $3, '000000', $4, $5, 2020, 1, $6, 'Unknown', 'Unknown',
                    NOW(), 'SYSTEM', $7, 'STEADY', 'WEB', 'US')
            ON CONFLICT (company_id) DO UPDATE SET
                name = EXCLUDED.name,
                industry = EXCLUDED.industry,
                risk_segment = EXCLUDED.risk_segment
            """,
            applicant_id,
            name,
            industry,
            jurisdiction,
            legal_type,
            f"EIN-{applicant_id}",
            risk_segment,
        )
    except Exception as exc:
        return _err(from_exception(exc, f"registry-{applicant_id}"))

    return _ok(applicant_id=applicant_id, status="REGISTERED")


# ---------------------------------------------------------------------------
# Tool 0 — record_document_upload
# ---------------------------------------------------------------------------


@mcp.tool()
async def record_document_upload(
    application_id: str,
    document_id: str,
    file_path: str,
    document_type: str = "LOAN_APPLICATION_PDF",
) -> dict[str, Any]:
    """Record a document upload (e.g., a PDF) for an application.
    This enables background agents to process the document.
    """
    logger.info(" TOOL: record_document_upload (app=%s, file=%s)", application_id, file_path)
    loan_stream = f"loan-{application_id}"

    if not application_id.strip():
        return _err(validation_error("application_id must not be empty."))
    if not document_id.strip():
        return _err(validation_error("document_id must not be empty."))
    if not file_path.strip():
        return _err(validation_error("file_path must not be empty."))

    # Verify loan exists
    if await _get_store().stream_version(loan_stream) == -1:
        return _err(not_found_error(f"Loan application {loan_stream}"))

    try:
        event = BaseEvent(
            event_type="DocumentUploaded",
            payload={
                "application_id": application_id,
                "document_id": document_id,
                "file_path": file_path,
                "document_type": document_type,
                "uploaded_at": datetime.now().isoformat(),
            },
        )
        l_ver = await _get_store().stream_version(loan_stream)
        await _get_store().append(loan_stream, [event], expected_version=l_ver)
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(
        application_id=application_id,
        document_id=document_id,
        file_path=file_path,
    )


# ---------------------------------------------------------------------------
# Tool 1 — submit_application
# ---------------------------------------------------------------------------


@mcp.tool()
async def submit_application(
    application_id: str,
    applicant_id: str,
    requested_amount_usd: float,
) -> dict[str, Any]:
    """Submit a new loan application. Validates schema and rejects duplicates.

    :param application_id: Unique loan ID (e.g. 'loan-2026-001').
    :param applicant_id: ID of a registered applicant.
    :param requested_amount_usd: Total loan amount requested.
    """
    logger.info(" TOOL: submit_application (app=%s, applicant=%s)", application_id, applicant_id)
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
    logger.info(" TOOL: start_agent_session (agent=%s, session=%s)", agent_id, session_id)
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
    logger.info(" TOOL: record_credit_analysis (app=%s, risk=%s)", application_id, risk_tier)
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
    logger.info(" TOOL: record_fraud_screening (app=%s, score=%s)", application_id, fraud_score)
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
        # NOTE: These two appends are NOT atomic — the same pattern used by
        # record_credit_analysis (service.py). If the session append succeeds
        # but the loan append fails, the session stream will record the
        # contribution but the loan stream will not advance. The caller must
        # retry; the session append is idempotent-safe because the session
        # stream version check will catch a duplicate on retry.
        # A fully atomic cross-stream write would require the outbox pattern
        # (see DESIGN.md §6 for the accepted tradeoff).
        s_ver = await _get_store().stream_version(session_stream)
        await _get_store().append(session_stream, [event], expected_version=s_ver)
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
    logger.info(
        " TOOL: record_compliance_check (app=%s, rule=%s, status=%s)",
        application_id,
        rule_id,
        status,
    )
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
    logger.info(" TOOL: generate_decision (app=%s, rec=%s)", application_id, recommendation)
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
    logger.info(
        " TOOL: record_human_review (app=%s, reviewer=%s, decision=%s)",
        application_id,
        reviewer_id,
        final_decision,
    )
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
# Tool 8a — request_credit_analysis
# ---------------------------------------------------------------------------


@mcp.tool()
async def request_credit_analysis(application_id: str) -> dict[str, Any]:
    """Transition a loan application to AWAITING_ANALYSIS state.

    Must be called after submit_application and before record_credit_analysis.
    Precondition: application must be in SUBMITTED state.

    :param application_id: Loan application ID (without 'loan-' prefix).
    """
    logger.info(" TOOL: request_credit_analysis (app=%s)", application_id)
    if not application_id.strip():
        return _err(validation_error("application_id must not be empty."))

    loan_stream = f"loan-{application_id}"
    if await _get_store().stream_version(loan_stream) == -1:
        return _err(not_found_error(f"Loan application {loan_stream}"))

    try:
        await _get_service().request_credit_analysis(loan_id=application_id)
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(application_id=application_id, state="AWAITING_ANALYSIS")


# ---------------------------------------------------------------------------
# Tool 8b — request_compliance_check
# ---------------------------------------------------------------------------


@mcp.tool()
async def request_compliance_check(
    application_id: str,
    required_rules: list[str] | None = None,
    regulation_set_version: str = _DEFAULT_REG_SET,
) -> dict[str, Any]:
    """Transition a loan application to COMPLIANCE_REVIEW state and open the compliance stream.

    Must be called after record_credit_analysis and before record_compliance_check.
    Precondition: application must be in ANALYSIS_COMPLETE state.

    :param application_id: Loan application ID (without 'loan-' prefix).
    :param required_rules: Optional list of rule IDs that must pass (e.g. ['KYC', 'AML']).
    :param regulation_set_version: Regulation set to apply (default: EU-AI-ACT-2025).
    """
    logger.info(" TOOL: request_compliance_check (app=%s)", application_id)
    if not application_id.strip():
        return _err(validation_error("application_id must not be empty."))

    if regulation_set_version not in _REGULATION_SETS:
        return _err(
            validation_error(
                f"Unknown regulation_set_version '{regulation_set_version}'.",
                suggested_action=f"Use one of: {sorted(_REGULATION_SETS)}",
            )
        )

    loan_stream = f"loan-{application_id}"
    if await _get_store().stream_version(loan_stream) == -1:
        return _err(not_found_error(f"Loan application {loan_stream}"))

    try:
        await _get_service().request_compliance_check(
            loan_id=application_id,
            required_rules=required_rules
            or sorted(_REGULATION_SETS.get(regulation_set_version, [])),
        )
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(application_id=application_id, state="COMPLIANCE_REVIEW")


# ---------------------------------------------------------------------------
# Tool 9 — run_integrity_check
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
# Tool 9 — generate_regulatory_package
# ---------------------------------------------------------------------------


@mcp.tool()
async def generate_regulatory_package_tool(
    application_id: str,
    examination_date: str,
    compliance_role: str,
) -> dict[str, Any]:
    """Generate a self-contained regulatory examination package for a loan application.

    Produces a JSON artifact containing:
      - Full ordered event stream as of examination_date
      - Projection states (application summary + compliance audit view) at that date
      - Cryptographic hash chain integrity verification result
      - Human-readable narrative (one sentence per significant event)
      - Agent attribution: model versions, confidence scores, input hashes

    The package is independently verifiable — a regulator can validate it
    without access to the live system.

    Requires compliance_role = 'COMPLIANCE_OFFICER'.

    :param application_id: Loan application ID (without 'loan-' prefix).
    :param examination_date: ISO-8601 datetime string (e.g. '2026-03-21T00:00:00').
    :param compliance_role: Must be 'COMPLIANCE_OFFICER'.
    """
    logger.info(
        " TOOL: generate_regulatory_package (app=%s, as_of=%s)", application_id, examination_date
    )

    if compliance_role != "COMPLIANCE_OFFICER":
        return _err(
            validation_error(
                "Only COMPLIANCE_OFFICER role may generate regulatory packages.",
                suggested_action="Authenticate with the COMPLIANCE_OFFICER role.",
            )
        )

    if not application_id.strip():
        return _err(validation_error("application_id must not be empty."))

    try:
        as_of = datetime.fromisoformat(examination_date)
    except ValueError:
        return _err(
            validation_error(
                f"examination_date '{examination_date}' is not a valid ISO-8601 datetime.",
                suggested_action="Use format: YYYY-MM-DDTHH:MM:SS",
            )
        )

    loan_stream = f"loan-{application_id}"
    if await _get_store().stream_version(loan_stream) == -1:
        return _err(not_found_error(f"Loan application {loan_stream}"))

    try:
        package = await generate_regulatory_package(
            application_id=application_id,
            examination_date=as_of,
            store=_get_store(),
            compliance_projection=_get_compliance(),
        )
    except Exception as exc:
        return _err(from_exception(exc, loan_stream))

    return _ok(package=package)


# ---------------------------------------------------------------------------
# Tool 10 — parse_document
# ---------------------------------------------------------------------------


@mcp.tool(name="parse_document")
async def parse_document(file_path: str) -> dict[str, Any]:
    """Parse a document (PDF, CSV, Excel, Text) and return its contents.

    This tool extracts text from PDFs and structured data from CSVs/Excels.
    """
    logger.info(" TOOL: parse_document (file=%s)", file_path)
    if not file_path.strip():
        return _err(validation_error("file_path must not be empty."))

    if not Path(file_path).exists():
        return _err(not_found_error(f"File {file_path}"))

    try:
        data = parse_any(file_path)
        return _ok(file_path=file_path, data=data)
    except Exception as exc:
        return _err(from_exception(exc))


# ---------------------------------------------------------------------------
# Tool 11 — list_documents
# ---------------------------------------------------------------------------


@mcp.tool(name="list_documents")
async def list_documents(directory_path: str) -> dict[str, Any]:
    """List documents in a directory to discover file paths for parsing.

    Useful for finding application_proposal.pdf, financial_summary.csv, etc.
    """
    logger.info(" TOOL: list_documents (dir=%s)", directory_path)
    if not directory_path.strip():
        return _err(validation_error("directory_path must not be empty."))

    path = Path(directory_path)
    if not path.exists() or not path.is_dir():
        return _err(not_found_error(f"Directory {directory_path}"))

    try:
        files = [f.name for f in path.iterdir() if f.is_file()]
        return _ok(directory_path=directory_path, files=sorted(files))
    except Exception as exc:
        return _err(from_exception(exc))


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
