"""Unit tests for the cryptographic audit chain."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ledger.core.audit_chain import compute_integrity_hash, verify_chain
from ledger.core.models import StoredEvent


def _make_event(
    event_type: str,
    payload: dict,  # type: ignore[type-arg]
    global_position: int,
    stream_position: int = 1,
) -> StoredEvent:
    return StoredEvent(
        event_id=uuid4(),
        event_type=event_type,
        payload=payload,
        stream_id="audit-loan-test",
        stream_position=stream_position,
        global_position=global_position,
        recorded_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# compute_integrity_hash
# ---------------------------------------------------------------------------


def test_hash_is_deterministic() -> None:
    e = _make_event("SomeEvent", {"x": 1}, global_position=1)
    h1 = compute_integrity_hash("", [e])
    h2 = compute_integrity_hash("", [e])
    assert h1 == h2


def test_hash_changes_with_different_previous_hash() -> None:
    e = _make_event("SomeEvent", {"x": 1}, global_position=1)
    h1 = compute_integrity_hash("aaa", [e])
    h2 = compute_integrity_hash("bbb", [e])
    assert h1 != h2


def test_hash_changes_when_payload_tampered() -> None:
    e1 = _make_event("SomeEvent", {"x": 1}, global_position=1)
    e2 = _make_event("SomeEvent", {"x": 2}, global_position=1)
    h1 = compute_integrity_hash("", [e1])
    h2 = compute_integrity_hash("", [e2])
    assert h1 != h2


def test_empty_event_list_produces_stable_hash() -> None:
    h = compute_integrity_hash("seed", [])
    assert len(h) == 64  # sha256 hex digest


# ---------------------------------------------------------------------------
# verify_chain
# ---------------------------------------------------------------------------


def test_verify_chain_no_checks_is_valid() -> None:
    ok, msg = verify_chain([], [])
    assert ok
    assert "No integrity checks" in msg


def test_verify_chain_single_check_valid() -> None:
    data_event = _make_event("ApplicationSubmitted", {"application_id": "a1"}, global_position=1)
    expected_hash = compute_integrity_hash("", [data_event])

    check_event = _make_event(
        "AuditIntegrityCheckRun",
        {"integrity_hash": expected_hash, "previous_hash": ""},
        global_position=2,
    )

    ok, msg = verify_chain([check_event], [data_event, check_event])
    assert ok, msg


def test_verify_chain_detects_tampered_payload() -> None:
    data_event = _make_event("ApplicationSubmitted", {"application_id": "a1"}, global_position=1)
    # Compute hash over original payload
    expected_hash = compute_integrity_hash("", [data_event])

    # Tamper: change the payload after hashing
    tampered = _make_event(
        "ApplicationSubmitted", {"application_id": "TAMPERED"}, global_position=1
    )

    check_event = _make_event(
        "AuditIntegrityCheckRun",
        {"integrity_hash": expected_hash, "previous_hash": ""},
        global_position=2,
    )

    ok, msg = verify_chain([check_event], [tampered, check_event])
    assert not ok
    assert "mismatch" in msg.lower()


def test_verify_chain_detects_broken_previous_hash() -> None:
    data_event = _make_event("SomeEvent", {"x": 1}, global_position=1)
    h1 = compute_integrity_hash("", [data_event])

    check1 = _make_event(
        "AuditIntegrityCheckRun",
        {"integrity_hash": h1, "previous_hash": ""},
        global_position=2,
    )

    data_event2 = _make_event("SomeEvent", {"x": 2}, global_position=3)
    h2 = compute_integrity_hash(h1, [data_event2])

    # Tamper: wrong previous_hash in check2
    check2 = _make_event(
        "AuditIntegrityCheckRun",
        {"integrity_hash": h2, "previous_hash": "WRONG"},
        global_position=4,
    )

    ok, msg = verify_chain([check1, check2], [data_event, check1, data_event2, check2])
    assert not ok
    assert "Chain broken" in msg


def test_verify_chain_two_valid_links() -> None:
    e1 = _make_event("E", {"n": 1}, global_position=1)
    h1 = compute_integrity_hash("", [e1])
    c1 = _make_event(
        "AuditIntegrityCheckRun",
        {"integrity_hash": h1, "previous_hash": ""},
        global_position=2,
    )

    e2 = _make_event("E", {"n": 2}, global_position=3)
    h2 = compute_integrity_hash(h1, [e2])
    c2 = _make_event(
        "AuditIntegrityCheckRun",
        {"integrity_hash": h2, "previous_hash": h1},
        global_position=4,
    )

    ok, msg = verify_chain([c1, c2], [e1, c1, e2, c2])
    assert ok, msg


# ---------------------------------------------------------------------------
# run_integrity_check (unit — mock store)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_integrity_check_appends_event() -> None:
    from unittest.mock import AsyncMock

    from ledger.core.audit_chain import run_integrity_check

    data_event = _make_event("ApplicationSubmitted", {"application_id": "a1"}, global_position=1)

    store = AsyncMock()
    # load_stream_raw called twice: once before append, once after for chain verification
    store.load_stream_raw = AsyncMock(return_value=[data_event])
    store.stream_version = AsyncMock(return_value=1)
    store.append = AsyncMock(return_value=2)

    result = await run_integrity_check("audit-loan-123", store)

    assert result.previous_hash == ""  # no prior check run
    assert len(result.integrity_hash) == 64  # sha256 hex
    assert result.chain_valid is True
    assert result.tamper_detected is False
    store.append.assert_called_once()

    # Verify the appended event payload
    appended_event = store.append.call_args[0][1][0]
    assert appended_event.event_type == "AuditIntegrityCheckRun"
    assert appended_event.payload["integrity_hash"] == result.integrity_hash
    assert appended_event.payload["previous_hash"] == ""
    assert appended_event.payload["events_hashed"] == 1


@pytest.mark.asyncio
async def test_run_integrity_check_chains_from_previous() -> None:
    from unittest.mock import AsyncMock

    from ledger.core.audit_chain import run_integrity_check

    e1 = _make_event("SomeEvent", {"x": 1}, global_position=1)
    h1 = compute_integrity_hash("", [e1])
    c1 = _make_event(
        "AuditIntegrityCheckRun",
        {"integrity_hash": h1, "previous_hash": ""},
        global_position=2,
    )
    e2 = _make_event("SomeEvent", {"x": 2}, global_position=3)

    store = AsyncMock()
    store.load_stream_raw = AsyncMock(return_value=[e1, c1, e2])
    store.stream_version = AsyncMock(return_value=3)
    store.append = AsyncMock(return_value=4)

    result = await run_integrity_check("audit-loan-123", store)

    assert result.previous_hash == h1  # chains from previous check
    expected = compute_integrity_hash(h1, [e2])
    assert result.integrity_hash == expected
