import asyncio
import uuid
from pathlib import Path

import asyncpg

from ledger.core.models import BaseEvent
from ledger.infrastructure.projections.agent_performance import AgentPerformanceProjection
from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.projections.daemon import ProjectionDaemon
from ledger.infrastructure.store import EventStore


async def run_load_test() -> None:
    # 1. Setup DB from .env or default
    dsn = "postgresql://postgres:password@localhost:5432/event_ledger"
    pool = await asyncpg.create_pool(dsn)
    store = EventStore(pool)

    # Register projections
    app_summary = ApplicationSummaryProjection(pool)
    agent_perf = AgentPerformanceProjection(pool)
    compliance = ComplianceAuditViewProjection(pool)

    # Setup DB Schema
    async with pool.acquire() as conn:
        schema_path = Path(__file__).parent / "../src/ledger/infrastructure/db/schema.sql"
        if schema_path.exists():
            schema_sql = schema_path.read_text()
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
            await conn.execute(schema_sql)

    daemon = ProjectionDaemon(
        store=store, projections=[app_summary, agent_perf, compliance], pool=pool, batch_size=1000
    )

    # 2. Start daemon in background
    daemon_task = asyncio.create_task(daemon.run_forever(poll_interval_ms=10))

    print("Pre-loading events...")
    # 3. Simulate burst of 100 applications
    for i in range(100):
        app_id = str(uuid.uuid4())
        event = BaseEvent(
            event_type="ApplicationSubmitted",
            payload={
                "application_id": app_id,
                "applicant_id": f"user-{i}",
                "requested_amount_usd": 5000.0,
            },
        )
        await store.append(f"application-{app_id}", [event], expected_version=-1)

        if i % 5 == 0:
            v = await store.stream_version(f"application-{app_id}")
            decision_event = BaseEvent(
                event_type="DecisionGenerated",
                payload={
                    "application_id": app_id,
                    "orchestrator_agent_id": "agent-alpha",
                    "model_versions": {"agent-alpha": "v1.0"},
                    "recommendation": "APPROVE",
                    "confidence_score": 0.85,
                },
            )
            await store.append(f"application-{app_id}", [decision_event], expected_version=v)

    print("Polling for lag to drop to 0...")
    for _ in range(40):  # 20 seconds max
        await asyncio.sleep(0.5)
        app_lag = await daemon.get_lag("ApplicationSummary")
        agent_lag = await daemon.get_lag("AgentPerformance")
        print(f"Current Lags: App={app_lag}, Agent={agent_lag}")
        if app_lag == 0 and agent_lag == 0:
            print("All caught up!")
            break

    # 4. Assert SLOs
    app_lag = await daemon.get_lag("ApplicationSummary")
    agent_lag = await daemon.get_lag("AgentPerformance")
    assert app_lag <= 5, f"ApplicationSummary lag too high: {app_lag}"
    assert agent_lag <= 5, f"AgentPerformance lag too high: {agent_lag}"

    daemon.stop()
    await daemon_task
    await pool.close()
    print("Verification PASSED")


if __name__ == "__main__":
    asyncio.run(run_load_test())
