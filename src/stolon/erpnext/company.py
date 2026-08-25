"""Company's default-account links.

ERPNext posts automatically to whichever account the Company record names, so a
chart applied without these set is a half-configured ledger. Which fields exist
varies by build -- this one has no `default_payroll_payable_account` at all --
so the target field names are checked against the live doctype before anything
is written. A silent no-op on a misspelled field is the failure mode worth
spending a round-trip to avoid.
"""

from __future__ import annotations

from typing import Any, Protocol


class UnknownCompanyFieldError(ValueError):
    """A default-account field this ERPNext build does not have."""


class _CompanyClient(Protocol):
    def get(self, doctype: str, name: str) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...


def account_link_fields(client: _CompanyClient) -> frozenset[str]:
    """Every Company field that links to an Account on this build."""
    meta = client.get("DocType", "Company")
    return frozenset(
        field["fieldname"]
        for field in meta.get("fields", [])
        if field.get("fieldtype") == "Link" and field.get("options") == "Account"
    )


def set_default_accounts(
    client: _CompanyClient,
    company: str,
    defaults: dict[str, str],
    *,
    known: frozenset[str],
) -> list[str]:
    """Point Company's defaults at specific accounts. Returns the fields written."""
    if not defaults:
        return []
    unknown = sorted(set(defaults) - known)
    if unknown:
        raise UnknownCompanyFieldError(
            f"Company on this site has no field(s): {', '.join(unknown)}; it offers {', '.join(sorted(known))}"
        )
    client.update("Company", company, dict(defaults))
    return sorted(defaults)
