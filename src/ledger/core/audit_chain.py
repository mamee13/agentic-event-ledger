"""Cryptographic audit chain for AuditLedger streams.

Algorithm:
    new_hash = sha256(previous_hash + sha256(payload_1) + sha256(payload_2) + ...)

The chain is verified by replaying all AuditIntegrityCheckRun events and
recomputing each hash. Any mismatch indicates tampering.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from ledger.core.models import BaseEvent, StoredEvent


def _hash_payload(payload: dict[str, Any]) -> str:
    """Deterministic SHA-256 of a JSON payload (sorted keys)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_integrity_hash(
    previous_hash: str,
    events: Sequence[StoredEvent | BaseEvent],
) -> str:
    """Computes new_hash = sha256(previous_hash + event_hashes).

    Each event contributes sha256(its payload). The concatenation is hashed
    once more to produce the chain link.
    """
    parts = [previous_hash] + [_hash_payload(e.payload) for e in events]
    combined = "".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def verify_chain(
    check_run_events: list[StoredEvent],
    all_events_by_position: list[StoredEvent],
) -> tuple[bool, str]:
    """Verifies the full hash chain from a list of AuditIntegrityCheckRun events.

    Args:
        check_run_events: All AuditIntegrityCheckRun events in order.
        all_events_by_position: All events on the audit stream in global_position order.

    Returns:
        (is_valid, message)
    """
    if not check_run_events:
        return True, "No integrity checks recorded yet."

    prev_hash = ""
    prev_position = 0

    for check in check_run_events:
        stored_previous = check.payload.get("previous_hash", "")
        stored_integrity = check.payload.get("integrity_hash", "")

        if stored_previous != prev_hash:
            return (
                False,
                f"Chain broken at position {check.global_position}: "
                f"expected previous_hash={prev_hash!r}, got {stored_previous!r}",
            )

        # Collect events between last check and this one
        window = [
            e
            for e in all_events_by_position
            if prev_position < e.global_position < check.global_position
        ]

        expected = compute_integrity_hash(prev_hash, window)
        if expected != stored_integrity:
            return (
                False,
                f"Hash mismatch at position {check.global_position}: "
                f"expected {expected!r}, stored {stored_integrity!r}",
            )

        prev_hash = stored_integrity
        prev_position = check.global_position

    return True, "Chain intact."


async def run_integrity_check(
    audit_stream_id: str,
    store: "EventStore",
) -> tuple[str, str]:
    """Appends an AuditIntegrityCheckRun event to the audit stream.

    Loads all events on the stream, finds the last AuditIntegrityCheckRun to
    get the previous_hash, hashes all events since that check, then appends
    the new check run event.

    Returns:
        (new_integrity_hash, previous_hash)
    """
    from ledger.core.models import BaseEvent

    events = await store.load_stream(audit_stream_id)

    # Find the last check run to get previous_hash and its position
    check_runs = [e for e in events if e.event_type == "AuditIntegrityCheckRun"]
    if check_runs:
        last_check = check_runs[-1]
        previous_hash: str = str(last_check.payload.get("integrity_hash", ""))
        last_check_position = last_check.global_position
    else:
        previous_hash = ""
        last_check_position = 0

    # Hash all events since the last check (excluding check runs themselves)
    window = [
        e
        for e in events
        if e.global_position > last_check_position and e.event_type != "AuditIntegrityCheckRun"
    ]

    new_hash = compute_integrity_hash(previous_hash, window)

    # Append the check run event
    current_version = await store.stream_version(audit_stream_id)
    expected_version = current_version if current_version != -1 else -1

    check_event = BaseEvent(
        event_type="AuditIntegrityCheckRun",
        payload={
            "integrity_hash": new_hash,
            "previous_hash": previous_hash,
            "events_hashed": len(window),
        },
    )
    await store.append(audit_stream_id, [check_event], expected_version=expected_version)

    return new_hash, previous_hash


# Avoid circular import — EventStore imports from this module indirectly
from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from ledger.infrastructure.store import EventStore
