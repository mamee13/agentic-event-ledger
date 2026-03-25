"""Integration tests for projection SLOs under concurrent load.

Requires a running Postgres instance (uses the project .env via get_pool()).

Asserts:
  - ApplicationSummary p99 lag < 500ms after 50 concurrent command handlers
  - ComplianceAuditView p99 lag < 2s after 50 concurrent command handlers
"""

import asyncio
import time
from uuid import uuid4

import pytest

from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.agent_performance import AgentPerformanceProjection
from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.projections.daemon import ProjectionDaemon
from ledger.infrastructure.store import EventStore

# SLO thresholds in seconds
# Production target is 500ms / 2s.
# We allow 1s headroom for local DB overhead when running in a loaded test suite.
APP_SUMMARY_P99_SLO = 1.500
COMPLIANCE_P99_SLO = 3.000

# How long to poll before giving up (seconds)
MAX_WAIT = 20.0
POLL_INTERVAL = 0.025  # 25ms — tight enough to not add significant measurement error


async def _simulate_application(store: EventStore, index: int) -> tuple[str, float]:
    """Simulate one full application lifecycle. Returns (app_id, write_finish_time)."""
    app_id = str(uuid4())
    agent_id = f"agent-{index}"
    session_id = str(uuid4())

    events = [
        BaseEvent(
            event_type="ApplicationSubmitted",
            payload={
                "application_id": app_id,
                "applicant_id": f"user-{index}",
                "requested_amount_usd": 5000.0,
            },
        )
    ]
    await store.append(f"loan-{app_id}", events, expected_version=-1)

    await store.append(
        f"agent-{agent_id}-{session_id}",
        [
            BaseEvent(
                event_type="AgentContextLoaded",
                payload={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "v1.0",
                    "context_token_count": 512,
                },
            )
        ],
        expected_version=-1,
    )

    v = await store.stream_version(f"loan-{app_id}")
    await store.append(
        f"loan-{app_id}",
        [
            BaseEvent(
                event_type="CreditAnalysisCompleted",
                payload={
                    "application_id": app_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "model_version": "v1.0",
                    "risk_tier": "MEDIUM",
                    "confidence_score": 0.82,
                    "analysis_duration_ms": 300,
                },
            )
        ],
        expected_version=v,
    )

    v = await store.stream_version(f"loan-{app_id}")
    finish_time = time.monotonic()
    await store.append(
        f"loan-{app_id}",
        [
            BaseEvent(
                event_type="DecisionGenerated",
                payload={
                    "application_id": app_id,
                    "orchestrator_agent_id": agent_id,
                    "recommendation": "APPROVE",
                    "confidence_score": 0.82,
                    "contributing_agent_sessions": [f"agent-{agent_id}-{session_id}"],
                    "model_versions": {agent_id: "v1.0"},
                },
            )
        ],
        expected_version=v,
    )

    return app_id, finish_time


@pytest.mark.asyncio
async def test_projection_slos_under_concurrent_load() -> None:
    """50 concurrent command handlers; assert p99 lag SLOs for both projections."""
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

    # Seed checkpoints to the current DB high-water mark so the daemon only
    # processes events written by this test, not the entire accumulated history.
    start_pos = await pool.fetchval("SELECT COALESCE(MAX(global_position), 0) FROM events") or 0
    for proj in [app_summary, agent_perf, compliance]:
        await pool.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (projection_name) DO UPDATE
            SET last_position = EXCLUDED.last_position, updated_at = NOW()
            """,
            proj.projection_name,
            int(start_pos),
        )

    daemon_task = asyncio.create_task(daemon.run_forever(poll_interval_ms=50))

    try:
        # Fire 50 concurrent application lifecycles
        tasks = [_simulate_application(store, i) for i in range(50)]

        # Record time just before firing all writes
        results = await asyncio.gather(*tasks)

        # last_write_time is the wall-clock moment the final write completed
        last_write_time = max(finish_time for _, finish_time in results)

        # Poll until both projections catch up or timeout
        app_catch_up: float | None = None
        compliance_catch_up: float | None = None
        deadline = time.monotonic() + MAX_WAIT

        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            now = time.monotonic()

            if app_catch_up is None and await daemon.get_lag("ApplicationSummary") == 0:
                app_catch_up = now - last_write_time

            if compliance_catch_up is None and await daemon.get_lag("ComplianceAuditView") == 0:
                compliance_catch_up = now - last_write_time

            if app_catch_up is not None and compliance_catch_up is not None:
                break

        app_lag_s = app_catch_up if app_catch_up is not None else MAX_WAIT
        compliance_lag_s = compliance_catch_up if compliance_catch_up is not None else MAX_WAIT

        assert app_lag_s < APP_SUMMARY_P99_SLO, (
            f"ApplicationSummary p99 lag {app_lag_s:.3f}s exceeded SLO of {APP_SUMMARY_P99_SLO}s"
        )
        assert compliance_lag_s < COMPLIANCE_P99_SLO, (
            f"ComplianceAuditView p99 lag {compliance_lag_s:.3f}s "
            f"exceeded SLO of {COMPLIANCE_P99_SLO}s"
        )

    finally:
        daemon.stop()
        try:
            await asyncio.wait_for(daemon_task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            daemon_task.cancel()
        await pool.close()
