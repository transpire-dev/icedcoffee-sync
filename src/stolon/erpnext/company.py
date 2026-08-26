"""Company's default-account links.

ERPNext posts automatically to whichever account the Company record names, so a
chart applied without these set is a half-configured ledger. Which fields exist
varies by build -- this one has no `default_payroll_payable_account` at all --
and Frappe accepts an update naming a field that does not exist without
complaint, silently dropping it rather than erroring.

`set_default_accounts` used to guard against that by checking the target field
names against `DocType` meta before writing. That meta read needs a
DocType-read permission a least-privilege bot cannot reliably hold: measured
live, a Custom DocPerm row granting read on `DocType` exists and reads back
from `permission_manager.get_permissions`, yet Frappe still answers
`has_permission("DocType", "", "read")` False and 403s `client.get("DocType",
...)` for that user -- a role permission on the `DocType` doctype itself does
not take effect the way it does for an ordinary doctype. So this reads the
Company document back after writing it and compares every requested field
against what is actually there instead: stronger than the meta check ever
was, since a field can exist and still refuse the value asked for, and it
needs nothing beyond the read/write access `set_default_accounts` already
required.
"""

from __future__ import annotations

from typing import Any, Protocol


class UnknownCompanyFieldError(ValueError):
    """A default-account field this ERPNext build does not have.

    Raised when a requested field is absent from the Company document read
    back after the write -- Frappe drops an update key naming no real field
    silently, so that absence is the only signal a misspelled field ever
    produces.
    """


class DefaultNotAppliedError(ValueError):
    """A default-account field exists but did not read back as requested.

    The field is real -- it is present in the document -- but the value
    stored is not the one just written, which a meta-only check (confirming
    the field exists) can never catch.
    """


class _CompanyClient(Protocol):
    def get(self, doctype: str, name: str) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...


def clear_defaults_pointing_at(client: _CompanyClient, company: str, account_names: frozenset[str]) -> list[str]:
    """Null out every Company default that currently points at one of `account_names`.

    For breaking the cycle around retiring an account a default still names:
    ERPNext refuses to disable an account that is any of Company's default
    links, and `coa apply` -- which would repoint the defaults at their
    replacements -- can itself be blocked by that same account occupying a
    chart number apply wants (`NUMBER_COLLISION`). This clears the pointer
    first, which only makes sense for a caller who has already accepted
    "defaults will be stranded until `coa apply` runs" -- `--allow-unapplied`
    prune, specifically.

    Looks for top-level fields whose value is a string exactly equal to one
    of `account_names`. An Account's document name (e.g. `"1110 - Cash -
    T"`) is specific enough -- the account name, and the company
    abbreviation it ends in, both baked in -- that it does not occur as the
    value of any field that is not itself a default-account link, so an
    exact string match is sufficient without needing to know which fields
    are Account-Link fields (this module no longer has a way to ask that;
    see the module docstring). `name` is skipped explicitly, and so is any
    key starting with `_` (Frappe's own metadata fields) -- neither is ever
    a default-account value, but a name collision is exactly the kind of
    thing this function's whole job is to not assume away.

    Frappe accepts null for an optional Link field. If a live run ever finds
    one of these fields mandatory, that surfaces as whatever `FrappeError`
    the server returns and propagates -- a field this function found
    pointing at a retiring account is never left alone silently.

    Returns:
        The cleared field names, sorted. No matches makes no request.
    """
    doc = client.get("Company", company)
    cleared = sorted(
        field
        for field, value in doc.items()
        if field != "name" and not field.startswith("_") and isinstance(value, str) and value in account_names
    )
    if not cleared:
        return []
    client.update("Company", company, dict.fromkeys(cleared))
    return cleared


def set_default_accounts(client: _CompanyClient, company: str, defaults: dict[str, str]) -> list[str]:
    """Point Company's defaults at specific accounts, then confirm each one actually landed.

    Writes `defaults` in a single `update`, then reads the Company document
    back and checks every requested field: absent means Frappe silently
    dropped an unknown field name; present but different means Frappe
    accepted the request but did not store the value asked for. Either is
    raised rather than left as a passing call that quietly did less than it
    said.

    Returns:
        The field names written, sorted. Empty input makes no request at
        all -- there is nothing to confirm either.
    """
    if not defaults:
        return []
    client.update("Company", company, dict(defaults))
    doc = client.get("Company", company)

    missing = sorted(field for field in defaults if field not in doc)
    if missing:
        raise UnknownCompanyFieldError(
            f"Company {company!r} has no field(s): {', '.join(missing)} -- "
            f"absent from the document read back after the write"
        )

    for field in sorted(defaults):
        wanted = defaults[field]
        got = doc[field]
        if got != wanted:
            raise DefaultNotAppliedError(f"{field}: wanted {wanted!r}, got {got!r} on Company {company!r}")

    return sorted(defaults)
