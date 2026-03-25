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
    record_compliance_check,
    record_credit_analysis,
    request_compliance_check,
    request_credit_analysis,
    start_agent_session,
    submit_application,
)


async def setup():
    pool = await get_pool()
    store = EventStore(pool)
    service = LedgerService(store)

    mcp_server._pool = pool
    mcp_server._store = store
    mcp_server._service = service
    mcp_server._app_summary = ApplicationSummaryProjection(pool)
    mcp_server._agent_perf = AgentPerformanceProjection(pool)
    mcp_server._compliance = ComplianceAuditViewProjection(pool)
    mcp_server._daemon = ProjectionDaemon(store, [], pool)


async def verify():
    await setup()
    app_id = str(uuid4())
    agent_id = "agent-1"
    session_id = str(uuid4())

    print(f"--- Testing Loan: {app_id} ---")

    # 1. Start Session
    print("1. Starting agent session...")
    await start_agent_session(agent_id, session_id, "v1.0")

    # 2. Submit Application
    print("2. Submitting application...")
    await submit_application(app_id, "applicant-1", 1000.0)

    # 3. Verify record_analysis FAILS without request
    print("3. Verifying record_credit_analysis fails in SUBMITTED state...")
    err = await record_credit_analysis(app_id, agent_id, session_id, "LOW", 0.9)
    if not err["ok"] and err["error"]["error_type"] == "DOMAIN_RULE_VIOLATION":
        print("   ✅ Correctly rejected: " + err["error"]["message"])
    else:
        print("   ❌ FAILED: Unexpected result:", err)

    # 4. Request Credit Analysis
    print("4. Requesting credit analysis (Transition to AWAITING_ANALYSIS)...")
    res = await request_credit_analysis(app_id)
    print(f"   Result: {res['status']}")

    # 5. Record Analysis
    print("5. Recording credit analysis (Transition to ANALYSIS_COMPLETE)...")
    res = await record_credit_analysis(app_id, agent_id, session_id, "LOW", 0.9)
    if res["ok"]:
        print("   ✅ Success")
    else:
        print("   ❌ FAILED:", res)

    # 6. Request Compliance
    print("6. Requesting compliance check (Transition to COMPLIANCE_REVIEW)...")
    res = await request_compliance_check(app_id, "EU-AI-ACT-2024")
    print(f"   Result: {res['status']}")

    # 7. Record Compliance
    print("7. Recording compliance results...")
    for rule in ["KYC", "AML", "FRAUD_SCREEN"]:
        res = await record_compliance_check(app_id, rule, "PASSED", "EU-AI-ACT-2024")
        print(f"   - {rule}: {res['ok']}")

    print("--- Verification Finished ---")
    await mcp_server._pool.close()


if __name__ == "__main__":
    asyncio.run(verify())
