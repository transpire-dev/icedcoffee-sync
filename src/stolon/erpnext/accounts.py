"""ERPNext Account facts: how accounts are named, and what types exist.

The constants here are a snapshot of ERPNext 16's Select options, measured on
2026-08-25. They exist so a chart can be validated offline, before any request
is sent. `stolon.erpnext.accounts.live_account_types` reads the same list from
a running site, and a live test asserts the two agree -- that is what catches an
ERPNext upgrade adding or removing a type.
"""

from __future__ import annotations

from typing import Any, Protocol

from stolon.apply.model import LiveAccount

ROOT_TYPES: frozenset[str] = frozenset({"Asset", "Liability", "Income", "Expense", "Equity"})

ACCOUNT_TYPES: frozenset[str] = frozenset({
    "Accumulated Depreciation",
    "Asset Received But Not Billed",
    "Bank",
    "Capital Work in Progress",
    "Cash",
    "Chargeable",
    "Cost of Goods Sold",
    "Current Asset",
    "Current Liability",
    "Depreciation",
    "Direct Expense",
    "Direct Income",
    "Equity",
    "Expense Account",
    "Expenses Included In Asset Valuation",
    "Expenses Included In Valuation",
    "Fixed Asset",
    "Income Account",
    "Indirect Expense",
    "Indirect Income",
    "Liability",
    "Payable",
    "Receivable",
    "Round Off",
    "Round Off for Opening",
    "Service Received But Not Billed",
    "Stock",
    "Stock Adjustment",
    "Stock Received But Not Billed",
    "Tax",
    "Temporary",
})


class UnknownAccountTypeError(ValueError):
    """An account_type outside ERPNext's Select options.

    Raised locally so a bad chart fails before a request is built, rather than
    surfacing as a server-side ValidationError halfway through an apply.
    """


def validate_account_type(value: str | None) -> None:
    """Refuse an account_type ERPNext would reject. Absent is allowed."""
    if not value:
        return
    if value not in ACCOUNT_TYPES:
        allowed = ", ".join(sorted(ACCOUNT_TYPES))
        raise UnknownAccountTypeError(f"unknown account_type {value!r}; ERPNext allows: {allowed}")


def document_name(account_name: str, abbr: str, account_number: str | None = None) -> str:
    """Derive an Account's document name the way ERPNext does.

    Numbered accounts are `{number} - {name} - {abbr}`; unnumbered ones drop
    the number and its separator. Frappe returns "" for an unset number, so an
    empty string counts as absent -- treating it as present would produce a
    leading " - " that matches no document.
    """
    if not account_name:
        raise ValueError("account_name must not be empty")
    if not abbr:
        raise ValueError("abbr must not be empty")
    if account_number:
        return f"{account_number} - {account_name} - {abbr}"
    return f"{account_name} - {abbr}"


_CHART_FIELDS = [
    "name",
    "account_number",
    "account_name",
    "parent_account",
    "is_group",
    "root_type",
    "account_type",
    "disabled",
]

# A chart this size has never been seen on a live site; it exists so a fuller
# chart is refused rather than silently truncated. A truncated read makes
# every account past the cutoff look absent from the site, and an absent
# account gets created again as a duplicate.
_MAX_CHART_ACCOUNTS = 5000


class _Reader(Protocol):
    """The slice of Client these reads need. Keeps the test doubles honest."""

    def list(self, doctype: str, **kwargs: Any) -> Any: ...
    def get(self, doctype: str, name: str) -> Any: ...


def company_abbr(client: _Reader, company: str) -> str:
    """The company's abbreviation, which every account's document name ends in."""
    rows = client.list("Company", fields=["name", "abbr"], filters=[["name", "=", company]], limit=1)
    if not rows:
        raise LookupError(f"no Company named {company!r} on this site")
    abbr = rows[0].get("abbr")
    if not abbr:
        raise LookupError(f"Company {company!r} has no abbreviation set")
    return str(abbr)


def read_chart(client: _Reader, company: str) -> list[LiveAccount]:
    """Every account on the site for one company.

    Frappe returns "" rather than null for unset links and selects, so both are
    normalised to None here. Leaving "" in place would make an unparented root
    look like a child of an account named "".

    Raises if the result exactly fills `_MAX_CHART_ACCOUNTS`: a single `list`
    call has no way to distinguish "the chart has precisely this many
    accounts" from "there are more the limit cut off", and a silent
    truncation reads every missing account as absent from the site -- which
    gets it created again as a duplicate.
    """
    rows = client.list(
        "Account",
        fields=_CHART_FIELDS,
        filters=[["company", "=", company]],
        limit=_MAX_CHART_ACCOUNTS,
        order_by="name asc",
    )
    if len(rows) == _MAX_CHART_ACCOUNTS:
        raise RuntimeError(
            f"Company {company!r} returned {_MAX_CHART_ACCOUNTS} accounts, "
            f"exactly the read cap -- the chart may be larger than that and "
            f"got silently truncated; raise _MAX_CHART_ACCOUNTS to confirm"
        )
    return [
        LiveAccount(
            document_name=row["name"],
            number=row.get("account_number") or "",
            name=row["account_name"],
            parent_document_name=row.get("parent_account") or None,
            is_group=bool(row.get("is_group")),
            root_type=row.get("root_type") or "",
            account_type=row.get("account_type") or None,
            disabled=bool(row.get("disabled")),
        )
        for row in rows
    ]


def accounts_with_entries(client: _Reader, document_names: list[str]) -> set[str]:
    """Which of these accounts already carry ledger entries.

    One `limit=1` presence probe per account, rather than a single batched
    `IN` query. What the caller needs is presence per account -- and a
    batched query has no way to give that: `list_documents` supports only
    fields, filters, limit, start and order_by, with no grouping or distinct
    clause to make "these accounts have zero entries" derivable from a capped
    page of rows. A shared `limit` over an `IN` filter caps total ROWS, not
    accounts -- one account with heavy history fills the whole page and
    starves the presence signal for every other name in the batch, reporting
    them as entry-free when they are not. That would let the caller modify
    accounts that carry real ledger history, which is exactly what this guard
    exists to prevent.

    The honest cost of that correctness: one request per account. This runs
    as part of a tool a human runs and reads, not a hot path.
    """
    found: set[str] = set()
    for name in document_names:
        rows = client.list(
            "GL Entry",
            fields=["account"],
            filters=[["account", "=", name]],
            limit=1,
        )
        if rows:
            found.add(name)
    return found


def live_account_types(client: _Reader) -> frozenset[str]:
    """The account_type Select options this site actually offers.

    Used by a live test to catch an ERPNext upgrade drifting from the checked-in
    ACCOUNT_TYPES snapshot.
    """
    meta = client.get("DocType", "Account")
    for field in meta.get("fields", []):
        if field.get("fieldname") == "account_type":
            return frozenset(o for o in (field.get("options") or "").split("\n") if o)
    raise LookupError("Account doctype has no account_type field")
