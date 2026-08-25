"""What we want, what is there, and how the two are matched.

Accounts are matched on `(account_name, parent account_name)` rather than on
account number. The number is the thing most likely to be changing -- a site
set up through ERPNext's wizard has no numbers at all -- so a number-based key
would match nothing and turn every renumber into a duplicate account. The name
plus its parent's name is what survives a renumber.

The pair is required because a name alone is not unique: a real chart carries a
group "Accounts Payable" with a leaf "Accounts Payable" inside it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

type MatchKey = tuple[str, str | None]


@dataclasses.dataclass(frozen=True)
class DesiredAccount:
    """One account as the chart file describes it."""

    number: str
    name: str
    parent_number: str | None
    is_group: bool
    root_type: str
    account_type: str | None = None


@dataclasses.dataclass(frozen=True)
class LiveAccount:
    """One account as the site currently has it."""

    document_name: str
    number: str
    name: str
    parent_document_name: str | None
    is_group: bool
    root_type: str
    account_type: str | None = None


def desired_key(account: DesiredAccount, by_number: dict[str, DesiredAccount]) -> MatchKey:
    """Match key for a desired account. Raises KeyError on a dangling parent."""
    if account.parent_number is None:
        return (account.name, None)
    parent = by_number.get(account.parent_number)
    if parent is None:
        raise KeyError(f"account {account.number} names parent {account.parent_number}, which is not in the chart")
    return (account.name, parent.name)


def live_keys(accounts: Iterable[LiveAccount]) -> dict[MatchKey, list[LiveAccount]]:
    """Index live accounts by match key.

    Collisions are kept as lists rather than resolved. Two live accounts
    sharing a key is real ambiguity, and the diff refuses it explicitly --
    keeping only the last one would silently pick a side.
    """
    accounts = list(accounts)
    by_document_name = {a.document_name: a for a in accounts}
    index: dict[MatchKey, list[LiveAccount]] = {}
    for account in accounts:
        if account.parent_document_name is None:
            key: MatchKey = (account.name, None)
        else:
            parent = by_document_name.get(account.parent_document_name)
            if parent is None:
                raise KeyError(
                    f"account {account.document_name!r} names parent "
                    f"{account.parent_document_name!r}, which is not in the given set"
                )
            key = (account.name, parent.name)
        index.setdefault(key, []).append(account)
    return index
