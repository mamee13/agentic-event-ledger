from ledger.core.errors import AgenticLedgerError, ConcurrencyError, ErrorCode


def test_ledger_error_dict() -> None:
    error = AgenticLedgerError(
        "Test error", code=ErrorCode.VALIDATION_ERROR, suggested_action="Fix it"
    )
    data = error.to_dict()
    assert data["error_code"] == "VALIDATION_ERROR"
    assert data["message"] == "Test error"
    assert data["suggested_action"] == "Fix it"


def test_concurrency_error() -> None:
    error = ConcurrencyError("stream-1", expected_version=5, actual_version=6)
    data = error.to_dict()
    assert data["error_code"] == "CONCURRENCY_CONFLICT"
    assert "stream-1" in data["message"]
    assert data["details"]["expected_version"] == 5
    assert data["details"]["actual_version"] == 6
