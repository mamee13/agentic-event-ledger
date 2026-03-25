"""Integration tests for domain invariants under concurrent write scenarios."""

import asyncio
from uuid import uuid4

import pytest

from ledger.core.errors import OptimisticConcurrencyError
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.store import EventStore


@pytest.mark.asyncio
async def test_concurrent_credit_analysis_enforces_single_write() -> None:
    """Two concurrent credit analyses must not both append to the loan stream.

    Invariant: Rule 3 (model version locking) + OCC ensures only one
    CreditAnalysisCompleted event exists for the loan without a human override.
    """
    pool = await get_pool()
    store = EventStore(pool)

    from ledger.application.service import LedgerService

    service = LedgerService(store)

    loan_id = str(uuid4())
    agent_a = "agent-a"
    agent_b = "agent-b"
    sess_a = str(uuid4())
    sess_b = str(uuid4())

    # Setup: submit loan + request analysis + start two sessions
    await service.submit_application(loan_id=loan_id, amount=25000.0, applicant_id="COMP-001")
    await service.request_credit_analysis(loan_id=loan_id)
    await service.start_agent_session(session_id=sess_a, agent_id=agent_a, model_version="v1.0")
    await service.start_agent_session(session_id=sess_b, agent_id=agent_b, model_version="v1.0")

    async def _run_analysis(agent_id: str, session_id: str) -> None:
        await service.record_credit_analysis(
            loan_id=loan_id,
            agent_id=agent_id,
            session_id=session_id,
            risk_score=0.75,
            reasoning="Concurrent test run.",
            risk_tier="MEDIUM",
            confidence_score=0.82,
        )

    results = await asyncio.gather(
        _run_analysis(agent_a, sess_a),
        _run_analysis(agent_b, sess_b),
        return_exceptions=True,
    )

    successes = [r for r in results if r is None]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}: {results}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}: {results}"
    assert isinstance(failures[0], OptimisticConcurrencyError), (
        "Expected OptimisticConcurrencyError on the losing writer."
    )

    # Loan stream should contain exactly one CreditAnalysisCompleted.
    loan_events = await store.load_stream(f"loan-{loan_id}")
    credit_events = [e for e in loan_events if e.event_type == "CreditAnalysisCompleted"]
    assert len(credit_events) == 1

    # Across both agent sessions, only one CreditAnalysisCompleted should exist.
    sess_a_events = await store.load_stream(f"agent-{agent_a}-{sess_a}")
    sess_b_events = await store.load_stream(f"agent-{agent_b}-{sess_b}")
    total_credit = sum(
        1 for e in sess_a_events + sess_b_events if e.event_type == "CreditAnalysisCompleted"
    )
    assert total_credit == 1

    await pool.close()
