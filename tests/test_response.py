import json
import pathlib

import pytest
from icedcoffee_sync.frappe.errors import DoesNotExistError, FrappeError, ValidationError
from icedcoffee_sync.frappe.response import MISSING, envelope, field, has_next_page, unwrap

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_unwrap_returns_the_data_payload() -> None:
    assert unwrap(200, json.dumps({"data": {"name": "Cash"}})) == {"name": "Cash"}


def test_unwrap_returns_lists_intact() -> None:
    assert unwrap(200, json.dumps({"data": [{"name": "Cash"}]})) == [{"name": "Cash"}]


def test_unwrap_raises_typed_errors_on_failure_status() -> None:
    body = json.dumps({"exc_type": "DoesNotExistError"})
    with pytest.raises(DoesNotExistError):
        unwrap(404, body)


def test_unwrap_raises_on_unparseable_success_body() -> None:
    with pytest.raises(FrappeError):
        unwrap(200, "<html>login page</html>")


def test_captured_error_fixture_raises_the_typed_error() -> None:
    captured = json.loads((FIXTURES / "error_not_found.json").read_text())
    with pytest.raises(DoesNotExistError):
        unwrap(captured["status"], captured["body"])


def test_captured_validation_fixture_raises_through_envelope() -> None:
    captured = json.loads((FIXTURES / "error_validation.json").read_text())
    with pytest.raises(ValidationError):
        envelope(captured["status"], captured["body"])


def test_absent_field_is_missing_not_none() -> None:
    # v2 includes empty fields, so absent and null mean different things:
    assert field({"account_name": "Cash"}, "account_number") is MISSING


def test_null_field_is_none_not_missing() -> None:
    assert field({"account_number": None}, "account_number") is None


def test_missing_is_falsy_and_distinct_from_none() -> None:
    assert MISSING is not None
    assert not MISSING


def test_captured_document_distinguishes_null_from_absent() -> None:
    # get_ok.json was captured specifically to prove this: 26 fields, 3 null,
    captured = json.loads((FIXTURES / "get_ok.json").read_text())
    doc = unwrap(captured["status"], captured["body"])
    assert doc["parent_account"] is None
    assert field(doc, "parent_account") is None
    assert field(doc, "no_such_field_at_all") is MISSING


def test_captured_list_fixture_unwraps_to_rows() -> None:
    captured = json.loads((FIXTURES / "list_ok.json").read_text())
    rows = unwrap(captured["status"], captured["body"])
    assert isinstance(rows, list)
    assert all("name" in row for row in rows)


def test_envelope_keeps_the_pagination_flag() -> None:
    body = json.dumps({"has_next_page": True, "data": [{"name": "Cash"}]})
    payload = envelope(200, body)
    assert payload["has_next_page"] is True
    assert payload["data"] == [{"name": "Cash"}]


def test_has_next_page_reads_the_flag() -> None:
    assert has_next_page({"has_next_page": True, "data": []}) is True
    assert has_next_page({"has_next_page": False, "data": []}) is False


def test_has_next_page_is_none_when_absent() -> None:
    # v1 responses carry no such flag; callers must fall back rather than
    assert has_next_page({"data": []}) is None


def test_has_next_page_is_none_when_explicitly_null() -> None:
    # A null flag is not the same as False -- both mean "server didn't say",
    assert has_next_page({"has_next_page": None, "data": []}) is None


def test_captured_list_fixture_carries_the_flag() -> None:
    captured = json.loads((FIXTURES / "list_ok.json").read_text())
    payload = envelope(captured["status"], captured["body"])
    assert has_next_page(payload) in (True, False)
