"""Compare a desired chart against a live one and say what would change.

The output separates four kinds of outcome deliberately:

- changes we will make (creates, renumbers, retypes),
- accounts on the site that the chart does not describe (extras), which are
  reported and left alone -- deletion is opt-in, never a consequence of a
  file not mentioning something,
- refusals, which are situations where more than one answer is defensible and
  guessing would touch a ledger.

A single refusal makes the whole plan unapplyable. That is the point of
plan-and-approve: a partially applied chart is worse than an unapplied one,
because the next run diffs against a state nobody chose.
"""

from __future__ import annotations

import dataclasses
import enum

from stolon.apply.model import DesiredAccount, LiveAccount, MatchKey, desired_key, live_keys


class RefusalReason(enum.StrEnum):
    """Why the diff declined to decide."""

    HAS_GL_ENTRIES = "has_gl_entries"
    AMBIGUOUS_MATCH = "ambiguous_match"
    GROUP_FLAG_CHANGED = "group_flag_changed"
    ROOT_TYPE_CHANGED = "root_type_changed"


@dataclasses.dataclass(frozen=True)
class CreateAccount:
    """An account in the chart with no counterpart on the site."""

    desired: DesiredAccount


@dataclasses.dataclass(frozen=True)
class RenumberAccount:
    """A matched account whose number differs. Executed as a rename."""

    live: LiveAccount
    desired: DesiredAccount


@dataclasses.dataclass(frozen=True)
class RetypeAccount:
    """A matched account whose account_type differs."""

    live: LiveAccount
    desired: DesiredAccount


@dataclasses.dataclass(frozen=True)
class ExtraAccount:
    """An account on the site that the chart does not describe."""

    live: LiveAccount


@dataclasses.dataclass(frozen=True)
class Refusal:
    """A situation the diff will not resolve on its own."""

    key: MatchKey
    reason: RefusalReason
    detail: str


@dataclasses.dataclass(frozen=True)
class ChartPlan:
    """Everything the diff concluded."""

    creates: list[CreateAccount]
    renumbers: list[RenumberAccount]
    retypes: list[RetypeAccount]
    extras: list[ExtraAccount]
    refusals: list[Refusal]

    @property
    def is_applyable(self) -> bool:
        """True when nothing was refused. Extras do not block."""
        return not self.refusals

    @property
    def is_empty(self) -> bool:
        """True when the site already matches the chart."""
        return not (self.creates or self.renumbers or self.retypes)


def diff(
    desired: list[DesiredAccount],
    live: list[LiveAccount],
    accounts_with_entries: set[str],
) -> ChartPlan:
    """Diff a desired chart against a live one.

    `accounts_with_entries` holds live document names carrying GL entries. Only
    accounts we would otherwise *change* are checked against it -- an account
    with history that already matches the chart is fine, and refusing it would
    make every subsequent run unapplyable forever.
    """
    by_number = {a.number: a for a in desired}
    index = live_keys(live)

    creates: list[CreateAccount] = []
    renumbers: list[RenumberAccount] = []
    retypes: list[RetypeAccount] = []
    refusals: list[Refusal] = []
    matched: set[str] = set()

    for want in desired:
        key = desired_key(want, by_number)
        candidates = index.get(key, [])

        if len(candidates) > 1:
            names = ", ".join(c.document_name for c in candidates)
            refusals.append(
                Refusal(key, RefusalReason.AMBIGUOUS_MATCH, f"{len(candidates)} live accounts match {key}: {names}")
            )
            matched.update(c.document_name for c in candidates)
            continue

        if not candidates:
            creates.append(CreateAccount(want))
            continue

        have = candidates[0]
        matched.add(have.document_name)

        if have.is_group != want.is_group:
            refusals.append(
                Refusal(
                    key,
                    RefusalReason.GROUP_FLAG_CHANGED,
                    f"{have.document_name} is_group={have.is_group}, chart says {want.is_group}",
                )
            )
            continue

        if have.root_type != want.root_type:
            refusals.append(
                Refusal(
                    key,
                    RefusalReason.ROOT_TYPE_CHANGED,
                    f"{have.document_name} root_type={have.root_type!r}, chart says {want.root_type!r}",
                )
            )
            continue

        wants_renumber = have.number != want.number
        wants_retype = (have.account_type or None) != (want.account_type or None)

        if (wants_renumber or wants_retype) and have.document_name in accounts_with_entries:
            refusals.append(
                Refusal(
                    key,
                    RefusalReason.HAS_GL_ENTRIES,
                    f"{have.document_name} carries ledger entries and cannot be modified",
                )
            )
            continue

        if wants_renumber:
            renumbers.append(RenumberAccount(have, want))
        if wants_retype:
            retypes.append(RetypeAccount(have, want))

    extras = [ExtraAccount(a) for a in live if a.document_name not in matched]
    return ChartPlan(creates, renumbers, retypes, extras, refusals)
