"""Company's default-account links.

ERPNext posts automatically to whichever account Company names, and Frappe
silently drops an update naming a field that doesn't exist. Validated by
reading Company itself, not `DocType` meta -- a role permission there
doesn't take effect the way it does elsewhere (measured live).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

# Company Link-to-Account fields without "account" in the name -- measured live.
_ACCOUNT_FIELD_NAME_EXCEPTIONS: frozenset[str] = frozenset({
    "round_off_for_opening",
    "stock_received_but_not_billed",
    "asset_received_but_not_billed",
})


def _plausibly_an_account_field(field: str) -> bool:
    """Whether `field` is plausibly a Link-to-Account field, by name (e.g. not `default_warehouse`)."""
    return "account" in field or field in _ACCOUNT_FIELD_NAME_EXCEPTIONS


class UnknownCompanyFieldError(ValueError):
    """A default-account field this ERPNext build does not have."""


class DefaultNotAppliedError(ValueError):
    """A default-account field exists but did not read back as requested."""


class _CompanyClient(Protocol):
    def get(self, doctype: str, name: str) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...


def clear_defaults_pointing_at(client: _CompanyClient, company: str, account_names: frozenset[str]) -> list[str]:
    """Null out every Company default pointing at one of `account_names`, for `--allow-unapplied` prune.

    Returns:
        The cleared field names, sorted. No matches makes no request.
    """
    doc = client.get("Company", company)
    cleared = sorted(
        field
        for field, value in doc.items()
        if field != "name"
        and not field.startswith("_")
        and isinstance(value, str)
        and value in account_names
        and _plausibly_an_account_field(field)
    )
    if not cleared:
        return []
    client.update("Company", company, dict.fromkeys(cleared))
    return cleared


def require_default_fields(client: _CompanyClient, company: str, fields: Iterable[str]) -> None:
    """Raise `UnknownCompanyFieldError` naming any of `fields` absent from `company`. A pre-flight; writes nothing."""
    doc = client.get("Company", company)
    missing = sorted(field for field in fields if field not in doc)
    if missing:
        raise UnknownCompanyFieldError(
            f"Company {company!r} has no field(s): {', '.join(missing)} -- "
            f"absent from the document read before applying anything"
        )


def set_default_accounts(client: _CompanyClient, company: str, defaults: dict[str, str]) -> list[str]:
    """Point Company's defaults at specific accounts, then confirm each one landed.

    Rejects any field absent from Company before writing, and reads back
    after to confirm each value actually stuck.

    Returns:
        The field names written, sorted. Empty input makes no request.
    """
    if not defaults:
        return []

    pre = client.get("Company", company)
    missing = sorted(field for field in defaults if field not in pre)
    if missing:
        raise UnknownCompanyFieldError(
            f"Company {company!r} has no field(s): {', '.join(missing)} -- "
            f"absent from the document read before writing anything"
        )

    client.update("Company", company, dict(defaults))
    doc = client.get("Company", company)

    for field in sorted(defaults):
        wanted = defaults[field]
        got = doc[field]
        if got != wanted:
            raise DefaultNotAppliedError(f"{field}: wanted {wanted!r}, got {got!r} on Company {company!r}")

    return sorted(defaults)
