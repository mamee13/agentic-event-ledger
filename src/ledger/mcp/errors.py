"""Structured MCP error objects.

Every tool error is a dict with exactly:
  error_type, message, stream_id, expected_version, actual_version, suggested_action
"""

from typing import Any

from ledger.core.errors import AgenticLedgerError, DomainRuleError, OptimisticConcurrencyError


def _make(
    error_type: str,
    message: str,
    stream_id: str = "",
    expected_version: int = -1,
    actual_version: int = -1,
    suggested_action: str = "",
) -> dict[str, Any]:
    return {
        "error_type": error_type,
        "message": message,
        "stream_id": stream_id,
        "expected_version": expected_version,
        "actual_version": actual_version,
        "suggested_action": suggested_action,
    }


def from_exception(exc: Exception, stream_id: str = "") -> dict[str, Any]:
    """Convert any Ledger exception into a structured MCP error dict."""
    if isinstance(exc, OptimisticConcurrencyError):
        d = exc.details
        return _make(
            error_type="CONCURRENCY_CONFLICT",
            message=exc.message,
            stream_id=str(d.get("stream_id", stream_id)),
            expected_version=int(d.get("expected_version", -1)),
            actual_version=int(d.get("actual_version", -1)),
            suggested_action=exc.suggested_action or "Reload stream and retry.",
        )
    if isinstance(exc, DomainRuleError):
        return _make(
            error_type="DOMAIN_RULE_VIOLATION",
            message=exc.message,
            stream_id=stream_id,
            suggested_action=exc.suggested_action or "",
        )
    if isinstance(exc, AgenticLedgerError):
        return _make(
            error_type=exc.code.value,
            message=exc.message,
            stream_id=stream_id,
            suggested_action=exc.suggested_action or "",
        )
    return _make(
        error_type="INTERNAL_ERROR",
        message=str(exc),
        stream_id=stream_id,
        suggested_action="Check server logs for details.",
    )


def validation_error(message: str, suggested_action: str = "") -> dict[str, Any]:
    return _make(error_type="VALIDATION_ERROR", message=message, suggested_action=suggested_action)


def not_found_error(resource: str) -> dict[str, Any]:
    return _make(
        error_type="RESOURCE_NOT_FOUND",
        message=f"{resource} not found.",
        suggested_action="Verify the ID and try again.",
    )


def rate_limit_error(message: str) -> dict[str, Any]:
    return _make(
        error_type="RATE_LIMITED",
        message=message,
        suggested_action="Wait before retrying.",
    )
