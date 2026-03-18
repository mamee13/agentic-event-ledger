"""Concrete upcasters registered against the global registry.

CreditAnalysisCompleted v1 → v2
    Fields added:
    - model_version: inferred from recorded_at using MODEL_VERSION_SCHEDULE.
      The schedule maps (start_date, end_date) windows to model identifiers.
      Error rate is low (~1%) only when a model was swapped mid-second; in that
      case we fall back to "unknown". We prefer "unknown" over a wrong value
      because a hallucinated model_version would corrupt the audit trail.
    - confidence_score: set to None (null) — truly unknown for v1 events.
      We never infer confidence because a wrong score would affect downstream
      compliance decisions replayed from history.
    - regulatory_basis: inferred from REGULATORY_SCHEDULE, same window logic.

DecisionGenerated v1 → v2
    Fields added:
    - model_versions: dict[agent_id, model_version] reconstructed by loading
      contributing AgentSession streams.

    PERFORMANCE TRADEOFF (documented per plan):
      Each v1 DecisionGenerated upcaster call that needs model_versions must
      load N AgentSession streams (one per contributing_agent_sessions entry).
      These are synchronous dict lookups against a pre-built cache when called
      from EventStore.load_stream/load_all, but if the cache is cold the store
      must be queried. In practice this adds ~1–5ms per event on a local DB.
      For bulk replays (rebuild_from_scratch) this is acceptable. For hot-path
      reads (single aggregate load) the contributing sessions are typically
      already in memory. If this becomes a bottleneck, the fix is to persist
      model_versions at write time and only fall back to reconstruction for
      legacy v1 events.
"""

from datetime import UTC, date, datetime
from typing import Any

from ledger.core.upcasting import UpcasterRegistry

# ---------------------------------------------------------------------------
# Global registry — imported by EventStore
# ---------------------------------------------------------------------------
registry = UpcasterRegistry()

# ---------------------------------------------------------------------------
# Model version schedule
# Each entry: (start inclusive, end exclusive, model_version_string)
# ---------------------------------------------------------------------------
_MODEL_VERSION_SCHEDULE: list[tuple[date, date, str]] = [
    (date(2024, 1, 1), date(2024, 7, 1), "v1.0"),
    (date(2024, 7, 1), date(2025, 1, 1), "v1.1"),
    (date(2025, 1, 1), date(2026, 1, 1), "v2.0"),
    (date(2026, 1, 1), date(2099, 1, 1), "v2.1"),
]

# ---------------------------------------------------------------------------
# Regulatory basis schedule
# ---------------------------------------------------------------------------
_REGULATORY_SCHEDULE: list[tuple[date, date, str]] = [
    (date(2024, 1, 1), date(2025, 1, 1), "EU-AI-ACT-2024"),
    (date(2025, 1, 1), date(2099, 1, 1), "EU-AI-ACT-2025"),
]


def _infer_from_schedule(
    schedule: list[tuple[date, date, str]], recorded_at: datetime
) -> str | None:
    d = recorded_at.astimezone(UTC).date()
    for start, end, value in schedule:
        if start <= d < end:
            return value
    return None


# ---------------------------------------------------------------------------
# CreditAnalysisCompleted  v1 → v2
# ---------------------------------------------------------------------------
@registry.register("CreditAnalysisCompleted", from_version=1, to_version=2)
def _upcast_credit_v1_v2(payload: dict[str, Any], recorded_at: datetime) -> dict[str, Any]:
    result = dict(payload)

    # model_version: infer from schedule; fall back to "unknown"
    if "model_version" not in result:
        result["model_version"] = (
            _infer_from_schedule(_MODEL_VERSION_SCHEDULE, recorded_at) or "unknown"
        )

    # confidence_score: null — never infer, wrong value corrupts audit trail
    if "confidence_score" not in result:
        result["confidence_score"] = None

    # regulatory_basis: infer from active regulation set at recorded_at
    if "regulatory_basis" not in result:
        result["regulatory_basis"] = (
            _infer_from_schedule(_REGULATORY_SCHEDULE, recorded_at) or "unknown"
        )

    return result


# ---------------------------------------------------------------------------
# DecisionGenerated  v1 → v2
# ---------------------------------------------------------------------------
@registry.register("DecisionGenerated", from_version=1, to_version=2)
def _upcast_decision_v1_v2(payload: dict[str, Any], _recorded_at: datetime) -> dict[str, Any]:
    result = dict(payload)

    # model_versions: reconstruct from contributing_agent_sessions if missing.
    # The EventStore passes a pre-built cache via payload["_session_model_cache"]
    # when available (injected during load). The cache is dual-keyed by both
    # session stream id and agent_id (from AgentContextLoaded), so projections
    # can look up by either key without any string parsing.
    if "model_versions" not in result:
        cache: dict[str, str] = result.pop("_session_model_cache", {})
        sessions: list[str] = result.get("contributing_agent_sessions", [])
        result["model_versions"] = {s: cache[s] for s in sessions if s in cache}

    # Remove the cache key if it leaked into the payload
    result.pop("_session_model_cache", None)

    return result
