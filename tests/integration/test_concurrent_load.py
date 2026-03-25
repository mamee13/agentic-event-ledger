"""Concurrent load test for optimistic concurrency under pressure."""

import asyncio
from uuid import uuid4

import pytest

from ledger.core.errors import OptimisticConcurrencyError
from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio
async def test_concurrent_writes_under_load() -> None:
    pool = await get_pool()
    store = EventStore(pool)

    stream_ids: list[str] = []
    for _ in range(20):
        app_id = str(uuid4())
        stream_id = f"loan-{app_id}"
        stream_ids.append(stream_id)
        await store.append(
            stream_id,
            [BaseEvent(event_type="ApplicationSubmitted", payload={"application_id": app_id})],
            expected_version=-1,
        )

    async def _competing_append(stream_id: str, tag: str, expected_version: int) -> None:
        await store.append(
            stream_id,
            [
                BaseEvent(
                    event_type="DecisionGenerated",
                    payload={"application_id": stream_id.removeprefix("loan-"), "tag": tag},
                )
            ],
            expected_version=expected_version,
        )

    # For each stream, race two concurrent writers.
    tasks = []
    for sid in stream_ids:
        v = await store.stream_version(sid)
        tasks.append(_competing_append(sid, "A", v))
        tasks.append(_competing_append(sid, "B", v))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    failures = [r for r in results if isinstance(r, OptimisticConcurrencyError)]
    assert failures, "Expected at least one OptimisticConcurrencyError under concurrent load."

    # Each stream should have exactly one of the competing events appended.
    for sid in stream_ids:
        events = await store.load_stream(sid)
        decisions = [e for e in events if e.event_type == "DecisionGenerated"]
        assert len(decisions) == 1

    await pool.close()
