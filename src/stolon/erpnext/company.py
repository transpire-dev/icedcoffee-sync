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
Company document itself instead, before writing anything, and checks every
requested field is present in it -- stronger than the meta check ever was,
and it needs nothing beyond the read/write access `set_default_accounts`
already required. A second read after the write then compares every value
against what was actually stored, since a field can exist and still refuse
the value asked for, which existence alone can never catch.

`require_default_fields` runs the same presence check standalone, for a
caller that wants to know before it does anything else at all -- `coa
apply` calls it before `apply_chart` writes a single Account, so a
misspelled or removed default field is caught before real chart changes
land, not after. `set_default_accounts` keeps its own read-before-write
check regardless: a caller of `set_default_accounts` alone, without going
through the pre-flight, is still protected right before its own write.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

# Company Link fields that point at Account but don't have "account" in
# their fieldname -- measured live on 2026-08-25 against this build's
# Company doctype: every other Link-to-Account field's name contains
# "account", so those three are the only exceptions `clear_defaults_pointing_at`
# needs to name explicitly. If a future ERPNext version adds another such
# field, it stays un-clearable here until this set is remeasured and updated.
_ACCOUNT_FIELD_NAME_EXCEPTIONS: frozenset[str] = frozenset({
    "round_off_for_opening",
    "stock_received_but_not_billed",
    "asset_received_but_not_billed",
})


def _plausibly_an_account_field(field: str) -> bool:
    """Whether `field` is a Company field `clear_defaults_pointing_at` may clear.

    A string match alone (see that function's docstring) is not enough to
    tell a default-account Link field apart from an ordinary string field
    that merely happens to equal the retiring account's name -- `default_warehouse`,
    for instance, is a Link to Warehouse, not Account, and would be cleared
    by exact-value matching alone if nothing else narrowed the field set.
    Restricting to fields whose name contains "account", plus the small
    named set of measured exceptions that link to Account without the word,
    keeps the match to fields plausibly meant as account pointers.
    """
    return "account" in field or field in _ACCOUNT_FIELD_NAME_EXCEPTIONS


class UnknownCompanyFieldError(ValueError):
    """A default-account field this ERPNext build does not have.

    Raised when a requested field is absent from a Company document read --
    by `require_default_fields` before any write happens at all, and again
    by `set_default_accounts` immediately before its own write, in case
    something changed between the two reads. Frappe drops an update key
    naming no real field silently, so that absence is the only signal a
    misspelled field ever produces.
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
    of `account_names`, restricted to fields `_plausibly_an_account_field`
    accepts (the fieldname contains "account", or is one of the measured
    exceptions in `_ACCOUNT_FIELD_NAME_EXCEPTIONS`). An Account's document
    name (e.g. `"1110 - Cash - T"`) is specific enough -- the account name,
    and the company abbreviation it ends in, both baked in -- that it is
    very unlikely to occur as the value of an unrelated field, but "unlikely"
    is not "impossible": a Link field to a different doctype (Warehouse,
    say) can happen to hold a value that reads identically to a retiring
    account's name, and clearing it would be a silent, unrelated data loss
    this function has no business causing. This module has no way to ask
    Frappe which fields are actually Link-to-Account (see the module
    docstring for why), so the fieldname filter is what stands in for that.
    `name` is skipped explicitly, and so is any key starting with `_`
    (Frappe's own metadata fields) -- neither is ever a default-account
    value, but a name collision is exactly the kind of thing this function's
    whole job is to not assume away.

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
    """Raise `UnknownCompanyFieldError` naming any of `fields` absent from `company`, before anything else runs.

    A pre-flight for a caller like `coa apply`, whose Company-defaults write
    (`set_default_accounts`) only happens after `apply_chart` has already
    created, renumbered, and retyped real Account documents. Without this,
    a misspelled or removed default field name is discovered only once
    those account changes have already landed -- leaving a partially
    applied chart behind a command that then exits on a Company-defaults
    error it could have caught first. Reads `company` once and checks every
    field in `fields` is present in it; writes nothing, so a caller that
    fails here has made no changes at all.
    """
    doc = client.get("Company", company)
    missing = sorted(field for field in fields if field not in doc)
    if missing:
        raise UnknownCompanyFieldError(
            f"Company {company!r} has no field(s): {', '.join(missing)} -- "
            f"absent from the document read before applying anything"
        )


def set_default_accounts(client: _CompanyClient, company: str, defaults: dict[str, str]) -> list[str]:
    """Point Company's defaults at specific accounts, then confirm each one actually landed.

    Reads the Company document first and rejects any field name absent from
    it before writing anything: a single `update` naming a mix of real and
    unknown fields is not all-or-nothing on Frappe's side -- it silently
    drops the unknown key while still writing the rest -- so validating only
    after the write would let the valid fields change even though this
    function then raises, leaving Company partially updated behind a
    command that reports failure.

    Once every field is confirmed to exist, writes `defaults` in a single
    `update`, then reads the document back again and checks every value:
    a field existing does not guarantee the write stuck (a validation hook
    could still refuse it), so this second read is not redundant with the
    first -- it is the same field-can-exist-and-still-refuse-the-value case
    the module docstring describes, unreachable from a meta check alone.

    Returns:
        The field names written, sorted. Empty input makes no request at
        all -- there is nothing to confirm either.
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
