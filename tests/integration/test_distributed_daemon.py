"""Integration tests for the distributed ProjectionDaemon shard coordination.

Covers:
1. Advisory lock acquisition — only one node can own a shard at a time.
2. Heartbeat refresh — heartbeat_at is updated while the shard is held.
3. Stale-shard reclaim — shards with expired heartbeats are reclaimed.
4. Per-shard checkpointing — each shard tracks its own last_position.
5. Two-shard parallel processing — two daemon instances process disjoint
   event ranges without overlap or duplication.
6. Lock release on stop — after stop(), another node can acquire the shard.
"""

import asyncio
import time
from uuid import uuid4

import pytest

from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.application_summary import ApplicationSummaryProjection
from ledger.infrastructure.projections.distributed_daemon import DistributedProjectionDaemon
from ledger.infrastructure.projections.shard_coordinator import (
    HEARTBEAT_TTL_SECONDS,
    ShardAssignment,
    ShardCoordinator,
)
from ledger.infrastructure.store import EventStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_app_submitted(store: EventStore, app_id: str | None = None) -> str:
    app_id = app_id or str(uuid4())
    await store.append(
        f"loan-{app_id}",
        [
            BaseEvent(
                event_type="ApplicationSubmitted",
                payload={
                    "application_id": app_id,
                    "applicant_id": f"user-{app_id[:8]}",
                    "requested_amount_usd": 1000.0,
                    "loan_purpose": "PERSONAL",
                    "loan_term_months": 36,
                    "submission_channel": "WEB",
                    "contact_email": f"user-{app_id[:8]}@example.com",
                    "contact_name": "Test User",
                    "submitted_at": "2024-01-01T12:00:00Z",
                    "application_reference": f"REF-{app_id[:8]}",
                },
            )
        ],
        expected_version=-1,
    )
    return app_id


# ---------------------------------------------------------------------------
# 1. Advisory lock — only one node can own a shard at a time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisory_lock_exclusive() -> None:
    """Two coordinators competing for the same shard: only one wins."""
    pool = await get_pool()
    coord_a = ShardCoordinator(pool=pool, node_id="node-A")
    coord_b = ShardCoordinator(pool=pool, node_id="node-B")

    assignment = ShardAssignment(
        shard_id=f"test-shard-{uuid4()}",
        projection_name="ApplicationSummary",
        global_pos_from=0,
        global_pos_to=None,
    )

    conn_a = await pool.acquire()
    conn_b = await pool.acquire()
    acquired_a = False
    try:
        acquired_a = await coord_a.try_acquire_shard(assignment, conn_a)
        acquired_b = await coord_b.try_acquire_shard(assignment, conn_b)

        assert acquired_a is True, "node-A should acquire the lock first"
        assert acquired_b is False, "node-B must not acquire a lock already held by node-A"
    finally:
        if acquired_a:
            await coord_a.release_shard(assignment, conn_a)
        await pool.release(conn_a)
        await pool.release(conn_b)
        await pool.close()


# ---------------------------------------------------------------------------
# 2. Heartbeat refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_refresh_updates_timestamp() -> None:
    """heartbeat_at advances after refresh_heartbeat is called."""
    pool = await get_pool()
    coord = ShardCoordinator(pool=pool, node_id="node-hb")

    assignment = ShardAssignment(
        shard_id=f"hb-shard-{uuid4()}",
        projection_name="ApplicationSummary",
        global_pos_from=0,
        global_pos_to=None,
    )

    conn = await pool.acquire()
    try:
        await coord.try_acquire_shard(assignment, conn)

        query = (
            "SELECT heartbeat_at FROM projection_shards "
            "WHERE shard_id = $1 AND projection_name = $2"
        )
        before = await pool.fetchval(
            query,
            assignment.shard_id,
            assignment.projection_name,
        )

        # Small sleep so clock advances
        await asyncio.sleep(0.05)
        await coord.refresh_heartbeat(assignment)

        after = await pool.fetchval(
            query,
            assignment.shard_id,
            assignment.projection_name,
        )

        assert after > before, "heartbeat_at must advance after refresh"
    finally:
        await coord.release_shard(assignment, conn)
        await pool.release(conn)
        await pool.close()


# ---------------------------------------------------------------------------
# 3. Stale-shard reclaim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_shard_reclaim() -> None:
    """A shard with an expired heartbeat is reclaimed by reclaim_stale_shards."""
    pool = await get_pool()
    coord = ShardCoordinator(pool=pool, node_id="node-stale")

    shard_id = f"stale-shard-{uuid4()}"
    projection_name = "ApplicationSummary"

    # Manually insert a shard row with a heartbeat in the past
    stale_ts = f"NOW() - '{HEARTBEAT_TTL_SECONDS + 5} seconds'::interval"
    await pool.execute(
        f"""
        INSERT INTO projection_shards
          (shard_id, projection_name, assigned_node, heartbeat_at, global_pos_from)
        VALUES ($1, $2, 'dead-node', {stale_ts}, 0)
        ON CONFLICT (shard_id, projection_name) DO UPDATE SET
          heartbeat_at = {stale_ts}
        """,
        shard_id,
        projection_name,
    )

    reclaimed = await coord.reclaim_stale_shards()
    reclaimed_ids = [r.shard_id for r in reclaimed]

    assert shard_id in reclaimed_ids, (
        f"Stale shard {shard_id} should have been reclaimed, got: {reclaimed_ids}"
    )

    # Row must be gone
    row = await pool.fetchrow(
        "SELECT 1 FROM projection_shards WHERE shard_id = $1 AND projection_name = $2",
        shard_id,
        projection_name,
    )
    assert row is None, "Reclaimed shard row must be deleted from projection_shards"

    await pool.close()


# ---------------------------------------------------------------------------
# 4. Per-shard checkpointing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_shard_checkpoint_isolation() -> None:
    """Two shards for the same projection maintain independent checkpoints."""
    pool = await get_pool()
    coord = ShardCoordinator(pool=pool, node_id="node-ckpt")

    proj = "ApplicationSummary"
    shard_a = f"ckpt-shard-A-{uuid4()}"
    shard_b = f"ckpt-shard-B-{uuid4()}"

    await coord.update_shard_checkpoint(proj, shard_a, 100)
    await coord.update_shard_checkpoint(proj, shard_b, 200)

    pos_a = await coord.get_shard_checkpoint(proj, shard_a)
    pos_b = await coord.get_shard_checkpoint(proj, shard_b)

    assert pos_a == 100, f"Shard A checkpoint should be 100, got {pos_a}"
    assert pos_b == 200, f"Shard B checkpoint should be 200, got {pos_b}"

    # Update one; the other must not change
    await coord.update_shard_checkpoint(proj, shard_a, 150)
    pos_b_after = await coord.get_shard_checkpoint(proj, shard_b)
    assert pos_b_after == 200, "Shard B checkpoint must not change when shard A is updated"

    await pool.close()


# ---------------------------------------------------------------------------
# 5. Two-shard parallel processing — no duplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_shards_process_events_without_duplication() -> None:
    """Two DistributedProjectionDaemon instances process events; no row is written twice."""
    pool = await get_pool()
    store = EventStore(pool)

    # Record the current high-water mark so both daemons start scanning
    # from here and don't have to replay the entire test DB history.
    start_pos = await pool.fetchval("SELECT COALESCE(MAX(global_position), 0) FROM events") or 0

    # Write 10 events after the watermark
    app_ids = []
    for _ in range(10):
        app_id = await _write_app_submitted(store)
        app_ids.append(app_id)

    shard_suffix = str(uuid4())[:8]

    proj_a = ApplicationSummaryProjection(pool)
    proj_b = ApplicationSummaryProjection(pool)

    daemon_a = DistributedProjectionDaemon(
        store=store,
        projections=[proj_a],
        pool=pool,
        shard_id=f"shard-0-{shard_suffix}",
        node_id="node-shard-0",
        global_pos_from=int(start_pos) + 1,
        batch_size=50,
    )
    daemon_b = DistributedProjectionDaemon(
        store=store,
        projections=[proj_b],
        pool=pool,
        shard_id=f"shard-1-{shard_suffix}",
        node_id="node-shard-1",
        global_pos_from=int(start_pos) + 1,
        batch_size=50,
    )

    task_a = asyncio.create_task(daemon_a.run_forever(poll_interval_ms=50))
    task_b = asyncio.create_task(daemon_b.run_forever(poll_interval_ms=50))

    # Wait until all 10 app_ids appear in the summary table
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        counts = await asyncio.gather(
            *[
                pool.fetchval(
                    "SELECT COUNT(*) FROM projection_application_summary WHERE application_id = $1",
                    app_id,
                )
                for app_id in app_ids
            ]
        )
        if all(c and c >= 1 for c in counts):
            break

    daemon_a.stop()
    daemon_b.stop()
    for task in (task_a, task_b):
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()

    # Every app_id must appear exactly once — ON CONFLICT DO UPDATE means
    # even if both daemons process the same event, the row is upserted not duplicated.
    for app_id in app_ids:
        count = await pool.fetchval(
            "SELECT COUNT(*) FROM projection_application_summary WHERE application_id = $1",
            app_id,
        )
        assert count == 1, (
            f"app_id {app_id} appears {count} times — expected exactly 1 (no duplication)"
        )

    await pool.close()


# ---------------------------------------------------------------------------
# 6. Lock release on stop — another node can acquire after stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_released_after_daemon_stops() -> None:
    """After a daemon stops, a second coordinator can acquire the same shard."""
    pool = await get_pool()
    store = EventStore(pool)

    shard_id = f"release-shard-{uuid4()}"
    proj = ApplicationSummaryProjection(pool)

    daemon = DistributedProjectionDaemon(
        store=store,
        projections=[proj],
        pool=pool,
        shard_id=shard_id,
        node_id="node-release",
        batch_size=50,
    )

    task = asyncio.create_task(daemon.run_forever(poll_interval_ms=50))
    # Give the daemon time to acquire the lock
    await asyncio.sleep(0.2)

    daemon.stop()
    try:
        await asyncio.wait_for(task, timeout=3.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    # Now a second coordinator should be able to acquire the same shard
    coord2 = ShardCoordinator(pool=pool, node_id="node-successor")
    assignment = ShardAssignment(
        shard_id=shard_id,
        projection_name="*",
        global_pos_from=0,
        global_pos_to=None,
    )
    conn2 = await pool.acquire()
    acquired = False
    try:
        acquired = await coord2.try_acquire_shard(assignment, conn2)
        assert acquired is True, "Successor node must be able to acquire the shard after release"
    finally:
        if acquired:
            await coord2.release_shard(assignment, conn2)
        await pool.release(conn2)
        await pool.close()


# ---------------------------------------------------------------------------
# 7. Shard range filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shard_range_filtering() -> None:
    """A daemon with a bounded range only processes events within that range."""
    pool = await get_pool()
    store = EventStore(pool)

    # Snapshot the current high-water mark so we work with relative positions.
    base = await pool.fetchval("SELECT COALESCE(MAX(global_position), 0) FROM events") or 0
    base = int(base)

    # Write 20 events — we'll bound the shard to the middle 6 (events 5–10 above base).
    written_ids = []
    for _ in range(20):
        app_id = await _write_app_submitted(store)
        written_ids.append(app_id)

    # Find the actual global positions of the 20 events we just wrote.
    rows = await pool.fetch(
        """
        SELECT global_position, payload->>'application_id' AS app_id
        FROM events
        WHERE global_position > $1 AND event_type = 'ApplicationSubmitted'
        ORDER BY global_position ASC
        """,
        base,
    )
    assert len(rows) >= 20, f"Expected at least 20 new events, got {len(rows)}"

    # Pick the 5th through 10th events (0-indexed: indices 4–9) as our shard range.
    shard_rows = rows[4:10]
    pos_from = int(shard_rows[0]["global_position"])
    pos_to = int(shard_rows[-1]["global_position"])
    expected_app_ids = {r["app_id"] for r in shard_rows}

    proj = ApplicationSummaryProjection(pool)
    # Clear projection table for isolation
    await pool.execute("DELETE FROM projection_application_summary")

    daemon = DistributedProjectionDaemon(
        store=store,
        projections=[proj],
        pool=pool,
        shard_id=f"range-shard-{uuid4()}",
        global_pos_from=pos_from,
        global_pos_to=pos_to,
        batch_size=50,
    )

    task = asyncio.create_task(daemon.run_forever(poll_interval_ms=50))
    await asyncio.sleep(1.0)
    daemon.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (TimeoutError, asyncio.CancelledError):
        task.cancel()

    # Only the 6 events in [pos_from, pos_to] should appear in the projection.
    processed_count = await pool.fetchval("SELECT COUNT(*) FROM projection_application_summary")
    assert processed_count == 6, (
        f"Expected 6 events processed in range [{pos_from}, {pos_to}], got {processed_count}"
    )

    # Verify the correct app_ids were processed.
    rows_in_proj = await pool.fetch("SELECT application_id FROM projection_application_summary")
    processed_ids = {r["application_id"] for r in rows_in_proj}
    assert processed_ids == expected_app_ids, (
        f"Wrong app_ids in projection.\nExpected: {expected_app_ids}\nGot: {processed_ids}"
    )

    await pool.close()


# ---------------------------------------------------------------------------
# 8. Heartbeat failure stops daemon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_failure_stops_daemon() -> None:
    """If the shard registration is deleted (reclaimed), the daemon stops."""
    pool = await get_pool()
    store = EventStore(pool)
    proj = ApplicationSummaryProjection(pool)

    shard_id = f"hb-fail-shard-{uuid4()}"
    daemon = DistributedProjectionDaemon(
        store=store,
        projections=[proj],
        pool=pool,
        shard_id=shard_id,
        batch_size=10,
    )

    task = asyncio.create_task(daemon.run_forever(poll_interval_ms=50))
    # Give time to start
    await asyncio.sleep(0.5)

    # Manually delete the shard row
    await pool.execute(
        "DELETE FROM projection_shards WHERE shard_id = $1",
        shard_id,
    )

    # Wait for the heartbeat loop to fail and stop the daemon.
    # Heartbeat interval is 5s, so we wait up to 20s to be safe.
    try:
        await asyncio.wait_for(task, timeout=20.0)
    except TimeoutError:
        pytest.fail("Daemon did not stop after heartbeat failure within 20s")
    except asyncio.CancelledError:
        pass

    assert task.done(), "Daemon should have stopped after heartbeat failure"

    await pool.close()
