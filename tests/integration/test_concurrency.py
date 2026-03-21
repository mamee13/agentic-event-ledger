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

    # 1. Seed the stream to version 3 (three events already appended)
    #    This matches the brief scenario: both agents read at version 3.
    seed_events = [
        BaseEvent(event_type="ApplicationSubmitted", payload={"amount": 50000}),
        BaseEvent(event_type="DocumentsUploaded", payload={"docs": ["id", "payslip"]}),
        BaseEvent(event_type="KYCPassed", payload={"score": 95}),
    ]
    await store.append(stream_id, seed_events, expected_version=-1)

    current_v = await store.stream_version(stream_id)
    assert current_v == 3

    # 2. Both agents read the stream at version 3 and attempt to append simultaneously
    event_agent_a = BaseEvent(
        event_type="CreditAnalysisCompleted", payload={"agent": "A", "risk": "low"}
    )
    event_agent_b = BaseEvent(
        event_type="CreditAnalysisCompleted", payload={"agent": "B", "risk": "medium"}
    )

    # Run appends concurrently — both pass expected_version=3
    results = await asyncio.gather(
        store.append(stream_id, [event_agent_a], expected_version=3),
        store.append(stream_id, [event_agent_b], expected_version=3),
        return_exceptions=True,
    )

    # 3. Assertions
    # One should succeed (return 4), one should fail (raise OptimisticConcurrencyError)
    successes = [r for r in results if isinstance(r, int)]
    failures = [r for r in results if isinstance(r, OptimisticConcurrencyError)]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}: {results}"
    assert successes[0] == 4

    # Verify final stream state: total events = 4 (3 seed + 1 winner)
    final_v = await store.stream_version(stream_id)
    assert final_v == 4

    events = await store.load_stream(stream_id)
    assert len(events) == 4

    # The winning event must occupy stream_position 4
    winning_event = events[-1]
    assert winning_event.stream_position == 4

    await pool.close()
