import asyncio
from uuid import uuid4

import pytest

from ledger.core.errors import OptimisticConcurrencyError
from ledger.core.models import BaseEvent
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio
async def test_double_decision_concurrency() -> None:
    """
    Two AI agents simultaneously attempt to append a CreditAnalysisCompleted event
    to the same loan application stream. Only one must succeed.
    """
    pool = await get_pool()
    store = EventStore(pool)

    stream_id = f"loan-{uuid4()}"

    # 1. Initial state: Stream at version 0 (after first event)
    initial_event = BaseEvent(event_type="ApplicationSubmitted", payload={"amount": 1000})
    await store.append(stream_id, [initial_event], expected_version=-1)

    current_v = await store.stream_version(stream_id)
    assert current_v == 1

    # 2. Simulate two agents reading version 1 and trying to append simultaneously
    event_agent_a = BaseEvent(
        event_type="CreditAnalysisCompleted", payload={"agent": "A", "risk": "low"}
    )
    event_agent_b = BaseEvent(
        event_type="CreditAnalysisCompleted", payload={"agent": "B", "risk": "medium"}
    )

    # Run appends concurrently
    results = await asyncio.gather(
        store.append(stream_id, [event_agent_a], expected_version=1),
        store.append(stream_id, [event_agent_b], expected_version=1),
        return_exceptions=True,
    )

    # 3. Assertions
    # One should succeed (return 2), one should fail (raise OptimisticConcurrencyError)
    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, OptimisticConcurrencyError)]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}: {results}"
    assert successes[0] == 2

    # Verify final stream state
    final_v = await store.stream_version(stream_id)
    assert final_v == 2

    events = await store.load_stream(stream_id)
    assert len(events) == 2

    await pool.close()
