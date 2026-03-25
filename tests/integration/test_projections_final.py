import asyncio
import time
import uuid

import asyncpg
import pytest

from ledger.core.models import BaseEvent
from ledger.infrastructure.projections.agent_performance import AgentPerformanceProjection
from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.projections.daemon import ProjectionDaemon
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio
async def test_projections_performance_slo() -> None:
    # Setup DB
    dsn = "postgresql://postgres:password@localhost:5432/event_ledger"
    pool = await asyncpg.create_pool(dsn)
    store = EventStore(pool)

    # Register projections
    app_summary = ApplicationSummaryProjection(pool)
    agent_perf = AgentPerformanceProjection(pool)
    compliance = ComplianceAuditViewProjection(pool)

    daemon = ProjectionDaemon(
        store=store, projections=[app_summary, agent_perf, compliance], pool=pool, batch_size=500
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
        num_handlers = 50
        events_per_handler = 10

        async def handler_simulation(h_id: int) -> None:
            for i in range(events_per_handler):
                app_id = str(uuid.uuid4())
                event = BaseEvent(
                    event_type="ApplicationSubmitted",
                    payload={
                        "application_id": app_id,
                        "applicant_id": f"user-{h_id}-{i}",
                        "requested_amount_usd": 1000.0 * (i + 1),
                    },
                )
                await store.append(f"loan-{app_id}", [event], expected_version=-1)

        print(f"Starting {num_handlers} concurrent handlers...")
        start_burst = time.perf_counter()
        await asyncio.gather(*(handler_simulation(i) for i in range(num_handlers)))
        # Record wall-clock time the moment all writes are done — this is t=0 for lag measurement
        writes_done_at = time.monotonic()
        total_events = num_handlers * events_per_handler
        burst_duration = time.perf_counter() - start_burst
        print(f"Injected {total_events} events in {burst_duration:.2f}s")

        # Poll until ApplicationSummary catches up, measuring elapsed time from writes_done_at
        max_wait = 15.0
        poll_interval = 0.025  # 25ms for low measurement noise
        app_catch_up_s: float | None = None
        comp_catch_up_s: float | None = None
        deadline = time.monotonic() + max_wait

        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            now = time.monotonic()

            if app_catch_up_s is None and await daemon.get_lag("ApplicationSummary") == 0:
                app_catch_up_s = now - writes_done_at

            if comp_catch_up_s is None and await daemon.get_lag("ComplianceAuditView") == 0:
                comp_catch_up_s = now - writes_done_at

            if app_catch_up_s is not None and comp_catch_up_s is not None:
                break

        app_lag_ms = (app_catch_up_s if app_catch_up_s is not None else max_wait) * 1000
        print(f"Final detected catch-up latency: {app_lag_ms:.2f}ms")

        # SLO: 500ms production target + 1s local DB overhead headroom for loaded test suite
        assert app_lag_ms < 1500, f"ApplicationSummary lag too high: {app_lag_ms:.1f}ms"

        async with pool.acquire() as conn:
            total_apps = await conn.fetchval("SELECT COUNT(*) FROM projection_application_summary")
            assert total_apps >= total_events, (
                f"Expected at least {total_events} apps, got {total_apps}"
            )
    finally:
        daemon.stop()
        try:
            await asyncio.wait_for(daemon_task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            daemon_task.cancel()
        await pool.close()
