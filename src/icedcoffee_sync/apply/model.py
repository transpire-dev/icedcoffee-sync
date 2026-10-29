"""What the chart wants, and what the site currently has.

Neither record carries a match key. Matching the two is a tree walk
(`icedcoffee_sync.apply.diff`), because an account has no usable intrinsic identity: a
name repeats across the chart ("Accounts Payable" is both a group and a leaf
inside it), and the number is the thing most likely to be absent or changing --
a site set up through ERPNext's wizard has no numbers at all. What identifies
an account is where it sits: which root it hangs under, then its account name
among that parent's actual children.
"""

from __future__ import annotations

import dataclasses

type MatchKey = tuple[str, str | None]
"""How a refusal names the account it is about: (account name, parent name)."""


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
    # Defaults False so every existing construction site -- test fixtures
    # included -- keeps working unchanged; only icedcoffee_sync.apply.prune reads it.
    disabled: bool = False
