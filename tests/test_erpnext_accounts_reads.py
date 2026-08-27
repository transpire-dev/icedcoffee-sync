"""Reading a live chart, with a fake client rather than a live site.

These are shape tests: they pin the *requests* we make and how responses are
folded into LiveAccount. What the server actually does is covered by the live
tests in Task 10 -- the lesson from building this client is that a double alone
never catches a wrong picture of the API.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from stolon.erpnext.accounts import accounts_with_entries, company_abbr, read_chart

# FakeClient defines a public `list` method, which shadows the builtin `list`
_Rows = list[dict[str, Any]]
_Responses = dict[str, _Rows]
_Calls = list[tuple[str, dict[str, Any]]]


class FakeClient:
    def __init__(self, responses: _Responses) -> None:
        self.responses = responses
        self.calls: _Calls = []

    def list(self, doctype: str, **kwargs: Any) -> _Rows:
        self.calls.append((doctype, kwargs))
        return self.responses.get(doctype, [])

    def iter_all(self, doctype: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        # A plain pass-through: real pagination (page-size capping,
        self.calls.append((doctype, kwargs))
        return iter(self.responses.get(doctype, []))

    def get(self, doctype: str, name: str) -> dict[str, Any]:
        # Satisfies the _Reader protocol's `get` method. live_account_types is
        return {"fields": [{"fieldname": "account_type", "options": ""}]}


def test_company_abbr_is_read_from_the_company_document() -> None:
    client = FakeClient({"Company": [{"name": "Acmebike Cycling", "abbr": "AB"}]})
    assert company_abbr(client, "Acmebike Cycling") == "AB"


def test_missing_company_is_an_error_not_an_empty_abbr() -> None:
    # An empty abbr would silently produce document names like "Assets - ",
    client = FakeClient({"Company": []})
    with pytest.raises(LookupError, match="Nope"):
        company_abbr(client, "Nope")


def test_read_chart_folds_rows_into_live_accounts() -> None:
    client = FakeClient({
        "Account": [
            {
                "name": "Bank Accounts - AB",
                "account_number": "",
                "account_name": "Bank Accounts",
                "parent_account": "",
                "is_group": 1,
                "root_type": "Asset",
                "account_type": "Bank",
            }
        ]
    })
    (account,) = read_chart(client, "Acmebike Cycling")
    assert account.document_name == "Bank Accounts - AB"
    assert account.is_group is True
    # Frappe sends "" for an unset link, not null. Left as "", a root account
    assert account.parent_document_name is None
    assert account.number == ""
    assert account.account_type == "Bank"


def test_read_chart_filters_to_the_company() -> None:
    client = FakeClient({"Account": []})
    read_chart(client, "Acmebike Cycling")
    _, kwargs = client.calls[0]
    assert ["company", "=", "Acmebike Cycling"] in kwargs["filters"]


def test_read_chart_reads_through_iter_all_not_a_single_capped_page() -> None:
    # A single `list` call with a fixed limit has no way to tell "the chart
    row = {
        "name": "x",
        "account_number": "",
        "account_name": "x",
        "parent_account": "",
        "is_group": 0,
        "root_type": "Asset",
        "account_type": None,
    }
    many_accounts = 12_000
    client = FakeClient({"Account": [row] * many_accounts})
    accounts = read_chart(client, "Acmebike Cycling")
    assert len(accounts) == many_accounts
    doctype, _ = client.calls[0]
    assert doctype == "Account"


class _ProbeClient:
    """A GL Entry double that answers a `=` presence probe per account."""

    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.calls: _Calls = []

    def list(self, doctype: str, **kwargs: Any) -> _Rows:
        self.calls.append((doctype, kwargs))
        if doctype != "GL Entry":
            return []
        _, _, account = kwargs["filters"][0]
        return [{"account": account}] if account in self.present else []

    def get(self, doctype: str, name: str) -> dict[str, Any]:
        return {"fields": []}

    def iter_all(self, doctype: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
        # accounts_with_entries only ever calls list(); satisfies _Reader.
        raise AssertionError("accounts_with_entries should never page")


def test_accounts_with_entries_is_empty_when_nothing_is_asked() -> None:
    client = FakeClient({})
    assert accounts_with_entries(client, []) == set()
    assert client.calls == []


def test_accounts_with_entries_issues_one_request_per_account() -> None:
    client = _ProbeClient(present=set())
    accounts_with_entries(client, ["A", "B", "C"])
    assert client.calls == [
        ("GL Entry", {"fields": ["account"], "filters": [["account", "=", name]], "limit": 1})
        for name in ("A", "B", "C")
    ]


def test_accounts_with_entries_distinguishes_present_from_absent() -> None:
    client = _ProbeClient(present={"Present Account"})
    assert accounts_with_entries(client, ["Present Account", "Absent Account"]) == {"Present Account"}


def test_accounts_with_entries_does_not_let_a_busy_account_mask_a_quiet_neighbour() -> None:
    class DominatingLedger:
        def __init__(self) -> None:
            self.calls: _Calls = []

        def list(self, doctype: str, **kwargs: Any) -> _Rows:
            self.calls.append((doctype, kwargs))
            if doctype != "GL Entry":
                return []
            _, op, value = kwargs["filters"][0]
            if op == "in":
                # A batched query would see nothing but "Busy Account" --
                return [{"account": "Busy Account"}] * kwargs.get("limit", 1)
            return [{"account": value}] if value in ("Busy Account", "Quiet Account") else []

        def get(self, doctype: str, name: str) -> dict[str, Any]:
            return {"fields": []}

        def iter_all(self, doctype: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
            raise AssertionError("accounts_with_entries should never page")

    client = DominatingLedger()
    # Both accounts genuinely carry entries; only "Busy Account" has enough
    result = accounts_with_entries(client, ["Busy Account", "Quiet Account"])
    assert result == {"Busy Account", "Quiet Account"}
    assert all(kwargs["filters"][0][1] == "=" for _, kwargs in client.calls)
