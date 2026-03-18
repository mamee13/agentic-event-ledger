"""Unit tests for UpcasterRegistry and the two concrete upcasters."""

from datetime import UTC, datetime

from ledger.core.upcasting import UpcasterRegistry
from ledger.infrastructure.upcasters import registry

# ---------------------------------------------------------------------------
# UpcasterRegistry mechanics
# ---------------------------------------------------------------------------


def test_registry_no_upcaster_returns_unchanged() -> None:
    reg = UpcasterRegistry()
    version, payload = reg.upcast("UnknownEvent", 1, {"x": 1}, datetime.now(UTC))
    assert version == 1
    assert payload == {"x": 1}


def test_registry_single_hop() -> None:
    reg = UpcasterRegistry()

    @reg.register("MyEvent", from_version=1, to_version=2)
    def up(payload: dict, _recorded_at: datetime) -> dict:  # type: ignore[type-arg]
        return {**payload, "added": True}

    version, payload = reg.upcast("MyEvent", 1, {"x": 1}, datetime.now(UTC))
    assert version == 2
    assert payload["added"] is True


def test_registry_chained_hops() -> None:
    reg = UpcasterRegistry()

    @reg.register("E", from_version=1, to_version=2)
    def up1(p: dict, _: datetime) -> dict:  # type: ignore[type-arg]
        return {**p, "v2": True}

    @reg.register("E", from_version=2, to_version=3)
    def up2(p: dict, _: datetime) -> dict:  # type: ignore[type-arg]
        return {**p, "v3": True}

    version, payload = reg.upcast("E", 1, {}, datetime.now(UTC))
    assert version == 3
    assert payload["v2"] is True
    assert payload["v3"] is True


def test_registry_already_at_latest_version() -> None:
    reg = UpcasterRegistry()

    @reg.register("E", from_version=1, to_version=2)
    def up(p: dict, _: datetime) -> dict:  # type: ignore[type-arg]
        return {**p, "added": True}

    # v2 event — no upcaster registered for v2, so returned as-is
    version, payload = reg.upcast("E", 2, {"x": 1}, datetime.now(UTC))
    assert version == 2
    assert "added" not in payload


def test_registry_does_not_mutate_original_payload() -> None:
    reg = UpcasterRegistry()

    @reg.register("E", from_version=1, to_version=2)
    def up(p: dict, _: datetime) -> dict:  # type: ignore[type-arg]
        p["mutated"] = True  # intentionally mutate the copy
        return p

    original = {"x": 1}
    reg.upcast("E", 1, original, datetime.now(UTC))
    # The registry passes a copy, so original must be untouched
    assert "mutated" not in original


# ---------------------------------------------------------------------------
# CreditAnalysisCompleted v1 → v2
# ---------------------------------------------------------------------------


def test_credit_v1_adds_model_version() -> None:
    recorded_at = datetime(2025, 6, 1, tzinfo=UTC)
    _, payload = registry.upcast("CreditAnalysisCompleted", 1, {}, recorded_at)
    assert "model_version" in payload
    assert payload["model_version"] == "v2.0"  # 2025 window


def test_credit_v1_adds_null_confidence_score() -> None:
    _, payload = registry.upcast("CreditAnalysisCompleted", 1, {}, datetime.now(UTC))
    assert "confidence_score" in payload
    assert payload["confidence_score"] is None


def test_credit_v1_adds_regulatory_basis() -> None:
    recorded_at = datetime(2025, 6, 1, tzinfo=UTC)
    _, payload = registry.upcast("CreditAnalysisCompleted", 1, {}, recorded_at)
    assert payload["regulatory_basis"] == "EU-AI-ACT-2025"


def test_credit_v1_does_not_overwrite_existing_fields() -> None:
    existing = {
        "model_version": "custom-v99",
        "confidence_score": 0.9,
        "regulatory_basis": "CUSTOM",
    }
    _, payload = registry.upcast("CreditAnalysisCompleted", 1, existing, datetime.now(UTC))
    assert payload["model_version"] == "custom-v99"
    assert payload["confidence_score"] == 0.9
    assert payload["regulatory_basis"] == "CUSTOM"


def test_credit_v2_not_upcasted_again() -> None:
    version, payload = registry.upcast(
        "CreditAnalysisCompleted", 2, {"model_version": "v2.0"}, datetime.now(UTC)
    )
    assert version == 2


# ---------------------------------------------------------------------------
# DecisionGenerated v1 → v2
# ---------------------------------------------------------------------------


def test_decision_v1_adds_model_versions_from_cache() -> None:
    payload_in = {
        "contributing_agent_sessions": ["agent-a-s1", "agent-b-s2"],
        "_session_model_cache": {"agent-a-s1": "v1.0", "agent-b-s2": "v2.0"},
    }
    _, payload = registry.upcast("DecisionGenerated", 1, payload_in, datetime.now(UTC))
    assert payload["model_versions"] == {"agent-a-s1": "v1.0", "agent-b-s2": "v2.0"}
    # Cache key must be stripped from the output payload
    assert "_session_model_cache" not in payload


def test_decision_v1_empty_model_versions_when_no_cache() -> None:
    payload_in = {"contributing_agent_sessions": ["agent-a-s1"]}
    _, payload = registry.upcast("DecisionGenerated", 1, payload_in, datetime.now(UTC))
    assert payload["model_versions"] == {}


def test_decision_v1_does_not_overwrite_existing_model_versions() -> None:
    payload_in = {
        "model_versions": {"agent-a-s1": "v3.0"},
        "contributing_agent_sessions": ["agent-a-s1"],
        "_session_model_cache": {"agent-a-s1": "v1.0"},
    }
    _, payload = registry.upcast("DecisionGenerated", 1, payload_in, datetime.now(UTC))
    assert payload["model_versions"] == {"agent-a-s1": "v3.0"}
