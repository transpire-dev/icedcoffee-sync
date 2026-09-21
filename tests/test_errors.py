import json
import pathlib

import pytest
from stolon.frappe.errors import (
    ChildExistsError,
    DoesNotExistError,
    DuplicateEntryError,
    FrappeError,
    LinkExistsError,
    PermissionError_,
    RateLimitError,
    TimestampMismatchError,
    ValidationError,
    error_from_response,
    parse_server_messages,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _v2(error_type: str, message: str = "boom", **extra: object) -> str:
    return json.dumps({"errors": [{"type": error_type, "message": message, **extra}]})


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("ValidationError", ValidationError),
        ("DoesNotExistError", DoesNotExistError),
        ("DuplicateEntryError", DuplicateEntryError),
        ("TimestampMismatchError", TimestampMismatchError),
    ],
)
def test_v2_type_selects_the_subclass(error_type: str, expected: type[FrappeError]) -> None:
    error = error_from_response(417, _v2(error_type))
    assert isinstance(error, expected)
    assert error.error_type == error_type
    assert error.status == 417


def test_v2_message_becomes_the_exception_message() -> None:
    error = error_from_response(404, _v2("DoesNotExistError", "Account X not found"))
    assert error.message == "Account X not found"


def test_v2_traceback_is_kept_but_is_not_the_message() -> None:
    # The server sends a full Python traceback. It is useful for debugging and
    trace = 'Traceback (most recent call last):\n  File "apps/frappe/app.py"...'
    error = error_from_response(417, _v2("ValidationError", "Name is required", exception=trace))
    assert error.message == "Name is required"
    assert error.server_traceback == trace
    assert "Traceback" not in error.message


def test_multiple_errors_are_all_retained() -> None:
    body = json.dumps({
        "errors": [{"type": "ValidationError", "message": "one"}, {"type": "ValidationError", "message": "two"}]
    })
    error = error_from_response(417, body)
    assert error.messages == ["one", "two"]
    assert error.message == "one"


def test_unknown_type_falls_back_to_the_base_class() -> None:
    error = error_from_response(500, _v2("SomethingNewError"))
    assert type(error) is FrappeError
    assert error.error_type == "SomethingNewError"


def test_legacy_exc_type_envelope_is_still_understood() -> None:
    # Older Frappe apps and /api/method/ endpoints still send this shape.
    body = json.dumps({
        "exc_type": "AuthenticationError",
        "_server_messages": json.dumps([json.dumps({"message": "Access not allowed", "title": "Message"})]),
    })
    error = error_from_response(401, body)
    assert error.error_type == "AuthenticationError"
    assert error.message == "Access not allowed"


def test_server_messages_are_doubly_encoded() -> None:
    raw = json.dumps([json.dumps({"message": "Account is required", "title": "Message"})])
    assert parse_server_messages(raw) == ["Account is required"]


def test_server_messages_tolerates_plain_strings() -> None:
    assert parse_server_messages(json.dumps(["just text"])) == ["just text"]


def test_server_messages_of_none_is_empty() -> None:
    assert parse_server_messages(None) == []


def test_server_messages_of_garbage_does_not_raise() -> None:
    assert parse_server_messages("not json at all") == []


def test_non_json_body_still_produces_an_error() -> None:
    error = error_from_response(502, "<html>Bad Gateway</html>")
    assert isinstance(error, FrappeError)
    assert error.status == 502
    assert "Bad Gateway" in error.message


def test_429_is_a_rate_limit_error_regardless_of_body() -> None:
    assert isinstance(error_from_response(429, "{}"), RateLimitError)


def test_captured_not_found_fixture_maps_to_does_not_exist() -> None:
    captured = json.loads((FIXTURES / "error_not_found.json").read_text())
    error = error_from_response(captured["status"], captured["body"])
    assert isinstance(error, DoesNotExistError)
    assert error.status == 404
    assert "not found" in error.message


def test_captured_validation_fixture_maps_to_validation_error() -> None:
    captured = json.loads((FIXTURES / "error_validation.json").read_text())
    error = error_from_response(captured["status"], captured["body"])
    assert isinstance(error, ValidationError)
    assert error.status == 417
    assert error.server_traceback is not None
    assert "Traceback" not in error.message


def test_nested_set_child_exists_maps_to_child_exists_error() -> None:
    # Measured against a live site: deleting a group Account that still has
    error = error_from_response(
        417, _v2("NestedSetChildExistsError", "Cannot delete 1000 - Assets - AB as it has child nodes")
    )
    assert isinstance(error, ChildExistsError)
    assert not isinstance(error, LinkExistsError)
    assert not isinstance(error, ValidationError)
    assert error.error_type == "NestedSetChildExistsError"
    assert error.status == 417
    assert "has child nodes" in error.message


def test_unmapped_417_is_read_as_a_validation_failure() -> None:
    # Measured against the live test site: inserting a Journal Entry for a
    # company with perpetual inventory raises ERPNext's
    # StockAccountInvalidTransaction, a frappe.ValidationError subclass this
    # map has never heard of. Frappe answers every validation failure with
    # 417, so the status says what the name does not.
    error = error_from_response(
        417,
        _v2("StockAccountInvalidTransaction", "Account: Stock In Hand - AB can only be updated via Stock Transactions"),
    )
    assert isinstance(error, ValidationError)
    assert error.error_type == "StockAccountInvalidTransaction"
    assert error.status == 417
    assert "can only be updated via Stock Transactions" in error.message


def test_an_unmapped_type_at_another_status_stays_the_base_class() -> None:
    # Only 417 means validation; a 500 carrying the same name is a site fault.
    error = error_from_response(500, _v2("StockAccountInvalidTransaction", "the site fell over"))
    assert type(error) is FrappeError
    assert error.status == 500


def test_a_permission_refusal_is_never_a_validation_error() -> None:
    error = error_from_response(403, _v2("PermissionError", "Not permitted"))
    assert isinstance(error, PermissionError_)
    assert not isinstance(error, ValidationError)


def test_legacy_exc_type_envelope_also_reads_417_as_validation() -> None:
    # `/api/method/` endpoints answer with exc_type instead of an errors list;
    # the status means the same thing on both surfaces.
    body = json.dumps({
        "exc_type": "StockAccountInvalidTransaction",
        "_server_messages": json.dumps([
            json.dumps({"message": "Account: Stock In Hand - AB can only be updated via Stock Transactions"})
        ]),
    })
    error = error_from_response(417, body)
    assert isinstance(error, ValidationError)
    assert error.error_type == "StockAccountInvalidTransaction"
