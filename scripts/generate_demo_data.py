import asyncio
from uuid import uuid4

import ledger.mcp.server as mcp_server
from ledger.application.service import LedgerService
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.agent_performance import AgentPerformanceProjection
from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.projections.daemon import ProjectionDaemon
from ledger.infrastructure.store import EventStore
from ledger.mcp.server import (
    generate_decision,
    record_compliance_check,
    record_credit_analysis,
    record_human_review,
    run_integrity_check,
    start_agent_session,
    submit_application,
)


async def main() -> None:
    pool = await get_pool()
    store = EventStore(pool)
    service = LedgerService(store)

    app_summary = ApplicationSummaryProjection(pool)
    agent_perf = AgentPerformanceProjection(pool)
    compliance = ComplianceAuditViewProjection(pool)
    daemon = ProjectionDaemon(
        store=store, projections=[app_summary, agent_perf, compliance], pool=pool
    )

    # Setup MCP state for tool calls
    mcp_server._pool = pool
    mcp_server._store = store
    mcp_server._service = service
    mcp_server._app_summary = app_summary
    mcp_server._agent_perf = agent_perf
    mcp_server._compliance = compliance
    mcp_server._daemon = daemon

    app_id = f"demo-{uuid4().hex[:8]}"
    agent_id = "agent-demo-01"
    session_id = f"sess-{uuid4().hex[:8]}"

    print(f"Generating data for app: {app_id}")

    await start_agent_session(agent_id, session_id, "v2.1")
    await submit_application(app_id, "applicant-123", 50000.0)

    # Transition to analysis
    loan = await service._load_loan(app_id)
    from ledger.core.models import BaseEvent

    await store.append(
        f"loan-{app_id}",
        [BaseEvent(event_type="CreditAnalysisRequested", payload={"application_id": app_id})],
        expected_version=loan.version,
    )

    await record_credit_analysis(
        app_id, agent_id, session_id, "LOW", 0.95, "Strong applicant profile"
    )

    # Transition to compliance
    await service.request_compliance_check(app_id)
    for rule in ("KYC", "AML", "FRAUD_SCREEN"):
        await record_compliance_check(app_id, rule, "PASSED")

    await generate_decision(
        app_id,
        agent_id,
        session_id,
        "APPROVE",
        0.95,
        [{"agent_id": agent_id, "session_id": session_id}],
    )
    await record_human_review(app_id, "senior-manager-1", "APPROVE")

    # Run integrity check
    await run_integrity_check("loan", app_id, "COMPLIANCE_OFFICER")

    # Final clearance (one of my new events)
    v = await store.stream_version(f"loan-{app_id}")
    await store.append(
        f"loan-{app_id}",
        [
            BaseEvent(
                event_type="ComplianceClearanceIssued",
                payload={
                    "application_id": app_id,
                    "cleared_by": "compliance-node-7",
                    "regulation_set": "EU-AI-ACT-2025",
                },
            )
        ],
        expected_version=v,
    )

    # Process projections
    await daemon._process_batch()

    print(f"DONE. App ID: {app_id}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
