# ruff: noqa
"""datagen/schema_validator.py — validates all generated events against EVENT_REGISTRY"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from collections import Counter
from typing import Any

from ledger.schema.events import EVENT_REGISTRY


class SchemaValidator:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.validated: int = 0

    def validate(self, stream_id: str, event_dict: dict[str, Any]) -> bool:
        et = event_dict.get("event_type")
        payload = event_dict.get("payload", {})
        if not isinstance(et, str):
            self.errors.append(f"[{stream_id}] Event type is not a string: {et}")
            return False

        cls = EVENT_REGISTRY.get(et)
        if not cls:
            self.errors.append(f"[{stream_id}] Unknown event_type: {et}")
            return False
        try:
            cls(event_type=et, **payload)
            self.validated += 1
            return True
        except Exception as e:
            self.errors.append(f"[{stream_id}] {et}: {e}")
            return False

    def report(self, events: list[tuple[str, dict[str, Any], str]] | None = None) -> str:
        lines = [f"  Events validated: {self.validated}", f"  Errors:           {len(self.errors)}"]
        if events:
            type_counts = Counter(e[1]["event_type"] for e in events)
            stream_counts = Counter(e[0].split("-")[0] for e in events)
            lines += [
                f"  Event types: {dict(sorted(type_counts.items()))}",
                f"  Stream types: {dict(sorted(stream_counts.items()))}",
            ]
        if self.errors:
            lines += ["  ERRORS:"] + [f"    {e}" for e in self.errors[:10]]
        return "\n".join(lines)

    def assert_valid(self) -> None:
        if self.errors:
            raise AssertionError("Schema validation failed:\n" + "\n".join(self.errors))
