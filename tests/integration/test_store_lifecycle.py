from uuid import uuid4

import pytest

from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio  # type: ignore[misc]
async def test_event_store_lifecycle() -> None:
    pool = await get_pool()
    store = EventStore(pool)

    stream_id = f"test-{uuid4()}"

    # 1. Append
    events = [
        BaseEvent(event_type="TestStarted", payload={"foo": "bar"}),
        BaseEvent(event_type="TestProgressed", payload={"step": 1}),
    ]
    await store.append(stream_id, events, expected_version=-1)

    # 2. Load stream
    loaded = await store.load_stream(stream_id)
    assert len(loaded) == 2
    assert loaded[0].event_type == "TestStarted"
    assert loaded[1].stream_position == 2

    # 3. Stream version
    v = await store.stream_version(stream_id)
    assert v == 2

    # 4. Global replay
    all_events = []
    async for e in store.load_all():
        all_events.append(e)
    assert len(all_events) >= 2

    # 5. Metadata and Archive
    meta = await store.get_stream_metadata(stream_id)
    assert meta.current_version == 2
    assert meta.archived_at is None

    await store.archive_stream(stream_id)
    meta_after = await store.get_stream_metadata(stream_id)
    assert meta_after.archived_at is not None

    await pool.close()
