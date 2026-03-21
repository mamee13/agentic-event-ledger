"""Integration test: rebuild_from_scratch zero-downtime path.

Proves that ComplianceAuditViewProjection.rebuild_from_scratch() can replay
all events into a shadow table and swap it atomically while live reads
continue to return consistent data — no gap, no downtime.

Test sequence:
1. Write compliance events for two applications.
2. Seed the projection normally (simulates the live state before rebuild).
3. Concurrently: start a rebuild AND issue live reads against the projection.
4. Assert all live reads returned valid data (no KeyError / empty result).
5. Assert the projection state after rebuild matches the expected compliance state.
"""

import asyncio
from uuid import uuid4

import pytest

from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.store import EventStore


async def _write_compliance_lifecycle(
    store: EventStore,
    app_id: str,
    rules: list[str],
) -> None:
    """Write a minimal compliance event sequence for one application."""
    compliance_stream = f"compliance-{app_id}"
    await store.append(
        compliance_stream,
        [BaseEvent(event_type="ComplianceCheckRequested", payload={"application_id": app_id})],
        expected_version=-1,
    )
    for rule in rules:
        v = await store.stream_version(compliance_stream)
        await store.append(
            compliance_stream,
            [
                BaseEvent(
                    event_type="ComplianceRulePassed",
                    payload={"application_id": app_id, "rule_id": rule, "rule_version": "1.0"},
                )
            ],
            expected_version=v,
        )


@pytest.mark.asyncio
async def test_rebuild_from_scratch_zero_downtime() -> None:
    """rebuild_from_scratch swaps tables atomically; live reads never see a gap."""
    pool = await get_pool()
    store = EventStore(pool)
    projection = ComplianceAuditViewProjection(pool)

    app_id_1 = str(uuid4())
    app_id_2 = str(uuid4())

    # 1. Write events for two applications
    await _write_compliance_lifecycle(store, app_id_1, ["KYC", "AML"])
    await _write_compliance_lifecycle(store, app_id_2, ["KYC", "FRAUD_SCREEN"])

    # 2. Seed the projection by handling events directly (simulates live daemon state)
    for app_id in (app_id_1, app_id_2):
        events = await store.load_stream(f"compliance-{app_id}")
        for event in events:
            if event.event_type in projection.subscribed_events:
                await projection.handle_event(event)

    # Verify pre-rebuild state is correct
    pre_state_1 = await projection.get_current_compliance(app_id_1)
    assert pre_state_1["status"] == "PASSED"

    # 3. Run rebuild and concurrent live reads simultaneously
    live_read_results: list[dict[str, object]] = []
    read_errors: list[Exception] = []

    async def live_reader() -> None:
        """Issue reads against the projection while rebuild is running."""
        for _ in range(10):
            try:
                state = await projection.get_current_compliance(app_id_1)
                live_read_results.append(state)
            except Exception as exc:
                read_errors.append(exc)
            await asyncio.sleep(0.005)  # 5ms between reads

    rebuild_task = asyncio.create_task(projection.rebuild_from_scratch(store))
    reader_task = asyncio.create_task(live_reader())

    await asyncio.gather(rebuild_task, reader_task)

    # 4. No live reads should have raised exceptions
    assert not read_errors, f"Live reads raised errors during rebuild: {read_errors}"

    # All reads that returned data must have a valid status (not empty/corrupt)
    for result in live_read_results:
        assert "status" in result, f"Live read returned invalid state: {result}"
        assert result["status"] in ("PASSED", "IN_PROGRESS", "PENDING", "FAILED"), (
            f"Unexpected status during rebuild: {result['status']}"
        )

    # 5. Post-rebuild state must be correct for both applications
    post_state_1 = await projection.get_current_compliance(app_id_1)
    assert post_state_1["status"] == "PASSED"
    rules_1 = post_state_1["rules"]
    assert isinstance(rules_1, dict)
    assert rules_1.get("KYC") == "PASSED"
    assert rules_1.get("AML") == "PASSED"

    post_state_2 = await projection.get_current_compliance(app_id_2)
    assert post_state_2["status"] == "PASSED"
    rules_2 = post_state_2["rules"]
    assert isinstance(rules_2, dict)
    assert rules_2.get("KYC") == "PASSED"
    assert rules_2.get("FRAUD_SCREEN") == "PASSED"

    await pool.close()


@pytest.mark.asyncio
async def test_rebuild_from_scratch_resets_checkpoint() -> None:
    """After rebuild, the projection checkpoint must reflect the latest global position."""
    pool = await get_pool()
    store = EventStore(pool)
    projection = ComplianceAuditViewProjection(pool)

    app_id = str(uuid4())
    await _write_compliance_lifecycle(store, app_id, ["KYC"])

    await projection.rebuild_from_scratch(store)

    # Checkpoint must be set to the max global_position in the rebuilt table
    checkpoint = await pool.fetchval(
        "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
        projection.projection_name,
    )
    max_pos = await pool.fetchval("SELECT MAX(global_position) FROM projection_compliance_history")

    # checkpoint should equal max_pos (or 0 if no compliance events exist yet)
    assert checkpoint is not None, "Checkpoint must be written after rebuild"
    if max_pos is not None:
        assert int(checkpoint) == int(max_pos), (
            f"Checkpoint {checkpoint} does not match max position {max_pos}"
        )

    await pool.close()
