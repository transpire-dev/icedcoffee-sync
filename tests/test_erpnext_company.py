"""Company default accounts: written, then confirmed by reading the document back."""

from typing import Any

import pytest
from stolon.erpnext.company import (
    DefaultNotAppliedError,
    UnknownCompanyFieldError,
    clear_defaults_pointing_at,
    require_default_fields,
    set_default_accounts,
)


class FakeClient:
    """`update` only records; `get` always answers `read_back`, independent of
    what was sent to `update` -- letting a test state exactly what the server
    is pretending the write produced, including a silently-dropped field or
    a value Frappe refused to store.
    """

    def __init__(self, read_back: dict[str, Any]) -> None:
        self.read_back = read_back
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        self.updates.append((name, dict(doc)))
        return {"name": name}

    def get(self, doctype: str, name: str) -> Any:
        return self.read_back


def test_a_field_that_lands_is_returned() -> None:
    client = FakeClient({"default_bank_account": "1111 - Primary Checking Account - AB"})
    written = set_default_accounts(
        client, "Acmebike Cycling", {"default_bank_account": "1111 - Primary Checking Account - AB"}
    )
    assert written == ["default_bank_account"]


def test_the_update_body_is_exactly_the_defaults_dict() -> None:
    client = FakeClient({"default_bank_account": "1111 - Primary Checking Account - AB"})
    set_default_accounts(client, "Acmebike Cycling", {"default_bank_account": "1111 - Primary Checking Account - AB"})
    assert client.updates == [("Acmebike Cycling", {"default_bank_account": "1111 - Primary Checking Account - AB"})]


def test_empty_defaults_makes_no_update_call() -> None:
    client = FakeClient({})
    assert set_default_accounts(client, "Acmebike Cycling", {}) == []
    assert client.updates == []


def test_a_field_absent_from_the_document_is_refused_before_any_write() -> None:
    # Frappe silently drops an update key that names no real field on the
    # doctype rather than rejecting the whole request, so a single update
    # naming one unknown field alongside real ones would still write the
    # real ones -- checking existence only after the write would let that
    # happen before this function raises. Reading first and refusing here
    # means no update is ever attempted for an unknown field.
    client = FakeClient({})
    with pytest.raises(UnknownCompanyFieldError, match="default_payroll_payable_account"):
        set_default_accounts(client, "Acmebike Cycling", {"default_payroll_payable_account": "2131 - Payroll - AB"})
    assert client.updates == []


def test_a_known_field_alongside_an_unknown_one_is_never_written() -> None:
    # The exact partial-update hazard the pre-write check exists to close:
    # without it, `default_bank_account` here would already be changed by
    # the time the missing `default_payroll_payable_account` was noticed.
    client = FakeClient({"default_bank_account": "1111 - Primary Checking Account - AB"})
    with pytest.raises(UnknownCompanyFieldError, match="default_payroll_payable_account"):
        set_default_accounts(
            client,
            "Acmebike Cycling",
            {
                "default_bank_account": "1111 - Primary Checking Account - AB",
                "default_payroll_payable_account": "2131 - Payroll - AB",
            },
        )
    assert client.updates == []


def test_a_field_that_reads_back_differently_is_refused() -> None:
    client = FakeClient({"default_bank_account": "SOMETHING ELSE - AB"})
    with pytest.raises(DefaultNotAppliedError) as exc_info:
        set_default_accounts(
            client, "Acmebike Cycling", {"default_bank_account": "1111 - Primary Checking Account - AB"}
        )
    message = str(exc_info.value)
    assert "default_bank_account" in message
    assert "1111 - Primary Checking Account - AB" in message
    assert "SOMETHING ELSE - AB" in message


# --- clear_defaults_pointing_at -------------------------------------------------------


def test_clear_defaults_finds_and_clears_exactly_the_matching_fields() -> None:
    client = FakeClient({
        "name": "Transpire",
        "default_cash_account": "1110 - Cash - T",
        "depreciation_expense_account": "5203 - Depreciation Expense - T",
        "country": "United States",
    })
    cleared = clear_defaults_pointing_at(
        client, "Transpire", frozenset({"1110 - Cash - T", "5203 - Depreciation Expense - T"})
    )
    assert cleared == ["default_cash_account", "depreciation_expense_account"]
    assert client.updates == [("Transpire", {"default_cash_account": None, "depreciation_expense_account": None})]


def test_clear_defaults_leaves_non_matching_values_untouched() -> None:
    client = FakeClient({"name": "Transpire", "default_cash_account": "1110 - Cash - T", "country": "United States"})
    cleared = clear_defaults_pointing_at(client, "Transpire", frozenset({"1110 - Cash - T"}))
    assert cleared == ["default_cash_account"]
    # Only the matching field is in the update body -- "country" is real,
    # but its value never matched an account name being retired.
    assert client.updates == [("Transpire", {"default_cash_account": None})]


def test_clear_defaults_makes_no_update_call_when_nothing_matches() -> None:
    client = FakeClient({"name": "Transpire", "default_cash_account": "1110 - Cash - T", "country": "United States"})
    cleared = clear_defaults_pointing_at(client, "Transpire", frozenset({"9999 - Retiring - T"}))
    assert cleared == []
    assert client.updates == []


def test_clear_defaults_never_clears_the_name_field() -> None:
    # A pathological case: Company's own document name happens to equal one
    # of the retiring account names. `name` must never be nulled regardless.
    client = FakeClient({"name": "1110 - Cash - T", "default_cash_account": "1110 - Cash - T"})
    cleared = clear_defaults_pointing_at(client, "1110 - Cash - T", frozenset({"1110 - Cash - T"}))
    assert cleared == ["default_cash_account"]
    assert "name" not in cleared
    assert client.updates == [("1110 - Cash - T", {"default_cash_account": None})]


def test_clear_defaults_skips_underscore_prefixed_metadata_fields() -> None:
    client = FakeClient({"_liked_by": "1110 - Cash - T", "default_cash_account": "1110 - Cash - T"})
    cleared = clear_defaults_pointing_at(client, "Transpire", frozenset({"1110 - Cash - T"}))
    assert cleared == ["default_cash_account"]


def test_clear_defaults_never_clears_a_field_not_plausibly_an_account_link() -> None:
    # Regression: exact-value matching alone would clear any string field
    # that happens to equal the retiring account's name, even one that
    # links to a different doctype entirely (default_warehouse -> Warehouse,
    # not Account). Only fields named like account links -- "account" in
    # the name, or one of the measured exceptions -- are eligible.
    client = FakeClient({
        "name": "Transpire",
        "default_warehouse": "1110 - Cash - T",
        "default_cash_account": "1110 - Cash - T",
    })
    cleared = clear_defaults_pointing_at(client, "Transpire", frozenset({"1110 - Cash - T"}))
    assert cleared == ["default_cash_account"]
    assert "default_warehouse" not in cleared
    assert client.updates == [("Transpire", {"default_cash_account": None})]


def test_clear_defaults_clears_the_measured_exceptions_without_the_word_account() -> None:
    # round_off_for_opening, stock_received_but_not_billed, and
    # asset_received_but_not_billed all link to Account despite not
    # containing "account" in their fieldname -- measured live 2026-08-25.
    client = FakeClient({
        "name": "Transpire",
        "round_off_for_opening": "5203 - Round Off - T",
    })
    cleared = clear_defaults_pointing_at(client, "Transpire", frozenset({"5203 - Round Off - T"}))
    assert cleared == ["round_off_for_opening"]


# --- require_default_fields -------------------------------------------------------


class _NoWriteClient(FakeClient):
    """A `FakeClient` that fails the test if `update` is ever called.

    `require_default_fields` never writes -- an `update` call reaching this
    fake means the pre-flight let a write through it should have refused
    before.
    """

    def update(self, doctype: str, name: str, doc: dict) -> Any:  # type: ignore[override]
        raise AssertionError("require_default_fields must never write")


def test_require_default_fields_raises_on_a_missing_field_before_any_write() -> None:
    client = _NoWriteClient({"default_bank_account": "1111 - Primary Checking Account - AB"})
    with pytest.raises(UnknownCompanyFieldError, match="default_payroll_payable_account"):
        require_default_fields(client, "Acmebike Cycling", ["default_bank_account", "default_payroll_payable_account"])
    assert client.updates == []


def test_require_default_fields_passes_when_every_field_is_present() -> None:
    client = _NoWriteClient({
        "default_bank_account": "1111 - Primary Checking Account - AB",
        "default_expense_account": "5000 - Cost of Goods Sold - AB",
    })
    require_default_fields(client, "Acmebike Cycling", ["default_bank_account", "default_expense_account"])
    assert client.updates == []


def test_multiple_fields_all_land_in_one_update() -> None:
    # The real caller writes eleven Company defaults in a single call.
    client = FakeClient({
        "default_bank_account": "1111 - Primary Checking Account - AB",
        "default_expense_account": "5000 - Cost of Goods Sold - AB",
        "default_payable_account": "2000 - Accounts Payable - AB",
    })
    written = set_default_accounts(
        client,
        "Acmebike Cycling",
        {
            "default_bank_account": "1111 - Primary Checking Account - AB",
            "default_expense_account": "5000 - Cost of Goods Sold - AB",
            "default_payable_account": "2000 - Accounts Payable - AB",
        },
    )
    assert len(client.updates) == 1
    assert written == [
        "default_bank_account",
        "default_expense_account",
        "default_payable_account",
    ]
