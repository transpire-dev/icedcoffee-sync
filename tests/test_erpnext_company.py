"""Company default accounts, and refusing to write a field that does not exist."""

from typing import Any

import pytest
from stolon.erpnext.company import (
    UnknownCompanyFieldError,
    account_link_fields,
    set_default_accounts,
)

META = {
    "fields": [
        {"fieldname": "default_bank_account", "fieldtype": "Link", "options": "Account"},
        {"fieldname": "default_expense_account", "fieldtype": "Link", "options": "Account"},
        {"fieldname": "default_payable_account", "fieldtype": "Link", "options": "Account"},
        {"fieldname": "country", "fieldtype": "Link", "options": "Country"},
        {"fieldname": "abbr", "fieldtype": "Data"},
    ]
}


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def get(self, doctype: str, name: str) -> Any:
        return META

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        self.updates.append((name, doc))
        return {"name": name}


def test_only_account_links_are_offered() -> None:
    assert account_link_fields(FakeClient()) == {
        "default_bank_account",
        "default_expense_account",
        "default_payable_account",
    }


def test_defaults_are_written_in_one_update() -> None:
    client = FakeClient()
    written = set_default_accounts(
        client,
        "Acmebike Cycling",
        {"default_bank_account": "1111 - Primary Checking Account - AB"},
        known=frozenset({"default_bank_account"}),
    )
    assert client.updates == [("Acmebike Cycling", {"default_bank_account": "1111 - Primary Checking Account - AB"})]
    assert written == ["default_bank_account"]


def test_a_field_this_build_does_not_have_is_refused() -> None:
    # Measured: this ERPNext build has no default_payroll_payable_account. A
    # silent no-op would leave the operator believing it was set.
    client = FakeClient()
    with pytest.raises(UnknownCompanyFieldError, match="default_payroll_payable_account"):
        set_default_accounts(
            client,
            "Acmebike Cycling",
            {"default_payroll_payable_account": "2131 - Payroll - AB"},
            known=frozenset({"default_bank_account"}),
        )
    assert client.updates == []


def test_nothing_to_set_makes_no_request() -> None:
    client = FakeClient()
    assert set_default_accounts(client, "Acmebike Cycling", {}, known=frozenset()) == []
    assert client.updates == []


def test_multiple_defaults_written_in_one_update() -> None:
    # The real caller writes eleven Company defaults in a single call.
    # Verify the one-update guarantee across multiple fields.
    client = FakeClient()
    written = set_default_accounts(
        client,
        "Acmebike Cycling",
        {
            "default_bank_account": "1111 - Primary Checking Account - AB",
            "default_expense_account": "5000 - Cost of Goods Sold - AB",
            "default_payable_account": "2000 - Accounts Payable - AB",
        },
        known=frozenset({
            "default_bank_account",
            "default_expense_account",
            "default_payable_account",
        }),
    )
    # Exactly one update recorded, not three
    assert len(client.updates) == 1
    # All three field/value pairs in that single update
    assert client.updates[0] == (
        "Acmebike Cycling",
        {
            "default_bank_account": "1111 - Primary Checking Account - AB",
            "default_expense_account": "5000 - Cost of Goods Sold - AB",
            "default_payable_account": "2000 - Accounts Payable - AB",
        },
    )
    # All three fields returned, sorted
    assert written == [
        "default_bank_account",
        "default_expense_account",
        "default_payable_account",
    ]
