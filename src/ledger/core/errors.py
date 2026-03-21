from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    CONCURRENCY_CONFLICT = "CONCURRENCY_CONFLICT"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    UPCASTING_FAILURE = "UPCASTING_FAILURE"
    INTEGRITY_VIOLATION = "INTEGRITY_VIOLATION"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AgenticLedgerError(Exception):
    """Base exception for all Ledger related errors."""

    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.INTERNAL_ERROR,
        details: dict[str, Any] | None = None,
        suggested_action: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}
        self.suggested_action = suggested_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.code.value,
            "message": self.message,
            "details": self.details,
            "suggested_action": self.suggested_action,
        }


class OptimisticConcurrencyError(AgenticLedgerError):
    """Raised when an optimistic concurrency check fails."""

    # Declared structured fields — accessible directly on the exception instance
    stream_id: str
    expected_version: int
    actual_version: int

    def __init__(self, stream_id: str, expected_version: int, actual_version: int):
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        message = (
            f"Concurrency conflict on stream {stream_id}. "
            f"Expected {expected_version}, found {actual_version}."
        )
        super().__init__(
            message=message,
            code=ErrorCode.CONCURRENCY_CONFLICT,
            details={
                "stream_id": stream_id,
                "expected_version": expected_version,
                "actual_version": actual_version,
            },
            suggested_action="Reload the stream and retry the command with the latest version.",
        )


class DomainError(AgenticLedgerError):
    """Base exception for all domain-related errors."""


class DomainRuleError(DomainError):
    """Raised when a business rule is violated in an aggregate."""

    def __init__(self, rule_name: str, message: str, suggested_action: str | None = None):
        super().__init__(
            message=message,
            code=ErrorCode.INVALID_STATE_TRANSITION,
            details={"rule_name": rule_name},
            suggested_action=suggested_action,
        )


class IntegrityError(AgenticLedgerError):
    """Raised when an integrity check (hash chain) fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(
            message=message,
            code=ErrorCode.INTEGRITY_VIOLATION,
            details=details,
            suggested_action="Perform a full audit check and notify the regulatory team.",
        )
