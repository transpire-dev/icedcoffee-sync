"""Compare a desired chart against a live one and say what would change.

Matching is a top-down walk of both trees, one matched pair at a time. An
account has no usable intrinsic key, so identity comes from position: which
root it hangs under, then its account name among that parent's actual
children.

Roots are matched by root_type. A company has exactly one root per root_type
and so does the chart, so for a root the root_type *is* the identity. Matching
roots by name instead would fail on every site ERPNext set up itself -- its
stock roots are named "Application of Funds (Assets)", "Source of Funds
(Liabilities)" and "Income" -- and the unmatched chart roots would become
creates, which ERPNext rejects outright: inserting an Account with no
`parent_account` is a MandatoryError. Roots are therefore never created.

Matching a child against its parent's *resolved* live identity is what makes a
renamed parent safe. An index built from the pre-apply state keys a live child
off its parent's old name, so the chart's identical child under the new name
matches nothing: the live subtree is reported as extras, the desired subtree as
creates, and applying that duplicates the whole branch. Worse, the second run
then matches cleanly and reports no changes -- an idempotency check passes over
an already-wrong chart.

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
from collections.abc import Iterator

from stolon.apply.model import DesiredAccount, LiveAccount, MatchKey


class RefusalReason(enum.StrEnum):
    """Why the diff declined to decide."""

    HAS_GL_ENTRIES = "has_gl_entries"
    AMBIGUOUS_MATCH = "ambiguous_match"
    GROUP_FLAG_CHANGED = "group_flag_changed"
    ROOT_TYPE_CHANGED = "root_type_changed"
    ROOT_NOT_UNIQUE = "root_not_unique"


@dataclasses.dataclass(frozen=True)
class CreateAccount:
    """An account in the chart with no counterpart on the site."""

    desired: DesiredAccount


@dataclasses.dataclass(frozen=True)
class RenumberAccount:
    """A matched account whose number or name differs. Executed as a rename."""

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
    """A situation the diff will not resolve on its own.

    `live_document_names` holds the live accounts the refusal is about -- one
    for a mismatch, several for an ambiguous match, none when the site has no
    account where the chart needs one. It is the only handle a caller has on
    those documents: there is no key index to look them up in.
    """

    key: MatchKey
    reason: RefusalReason
    detail: str
    live_document_names: tuple[str, ...] = ()


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

    Args:
        desired: The chart as the file describes it.
        live: Every account the site has for the company.
        accounts_with_entries: Live document names carrying ledger entries.

    Returns:
        Everything the walk concluded, refusals included.

    Raises:
        KeyError: A desired account names a parent outside the chart, or a live
            account names a parent outside `live`.
        ValueError: The chart declares two roots of the same root_type, which
            leaves the root match with no way to tell them apart.
    """
    walk = _Walk(
        with_entries=accounts_with_entries,
        keys=_keys(desired),
        desired_children=_desired_children(desired),
        live_children=_live_children(live),
    )
    walk.roots()
    extras = [ExtraAccount(a) for a in live if a.document_name not in walk.matched]
    return ChartPlan(walk.creates, walk.renumbers, walk.retypes, extras, walk.refusals)


@dataclasses.dataclass
class _Walk:
    """One diff in progress: both trees, and what the walk has concluded so far."""

    with_entries: set[str]
    keys: dict[str, MatchKey]
    desired_children: dict[str | None, list[DesiredAccount]]
    live_children: dict[str | None, list[LiveAccount]]
    creates: list[CreateAccount] = dataclasses.field(default_factory=list)
    renumbers: list[RenumberAccount] = dataclasses.field(default_factory=list)
    retypes: list[RetypeAccount] = dataclasses.field(default_factory=list)
    refusals: list[Refusal] = dataclasses.field(default_factory=list)
    matched: set[str] = dataclasses.field(default_factory=set)

    def roots(self) -> None:
        """Match each chart root to the live root of the same root_type."""
        live_roots: dict[str, list[LiveAccount]] = {}
        for root in self.live_children.get(None, []):
            live_roots.setdefault(root.root_type, []).append(root)

        declared: dict[str, DesiredAccount] = {}
        for want in self.desired_children.get(None, []):
            if want.root_type in declared:
                raise ValueError(
                    f"chart roots {declared[want.root_type].number} and {want.number} are both "
                    f"root_type {want.root_type!r}; a root is matched by root_type alone, so two "
                    f"of a kind cannot be told apart"
                )
            declared[want.root_type] = want

            candidates = live_roots.get(want.root_type, [])
            if len(candidates) == 1:
                self.pair(want, candidates[0])
                continue

            found = ", ".join(c.document_name for c in candidates) or "none"
            self.refuse(
                want,
                RefusalReason.ROOT_NOT_UNIQUE,
                f"chart root {want.number} {want.name!r} needs exactly one live root of root_type "
                f"{want.root_type!r}; the site has {len(candidates)} ({found}). A root cannot be "
                f"created: ERPNext rejects an Account with no parent_account",
                candidates,
            )
            self.consume(candidates)

    def pair(self, want: DesiredAccount, have: LiveAccount) -> None:
        """Reconcile one matched pair, then match their children.

        The children are walked even when the pair itself was refused: the two
        accounts are the same account either way, so their subtrees are worth
        reporting. Nothing runs from a refused plan, so a create proposed under
        a disputed parent is information, not a hazard.
        """
        self.matched.add(have.document_name)
        self.reconcile(want, have)
        self.children(want, have)

    def reconcile(self, want: DesiredAccount, have: LiveAccount) -> None:
        """Decide what changes on a matched pair, or refuse to."""
        if have.is_group != want.is_group:
            self.refuse(
                want,
                RefusalReason.GROUP_FLAG_CHANGED,
                f"{have.document_name} is_group={have.is_group}, chart says {want.is_group}",
                [have],
            )
            return

        if have.root_type != want.root_type:
            self.refuse(
                want,
                RefusalReason.ROOT_TYPE_CHANGED,
                f"{have.document_name} root_type={have.root_type!r}, chart says {want.root_type!r}",
                [have],
            )
            return

        wants_rename = have.number != want.number or have.name != want.name
        wants_retype = (have.account_type or None) != (want.account_type or None)

        if (wants_rename or wants_retype) and have.document_name in self.with_entries:
            self.refuse(
                want,
                RefusalReason.HAS_GL_ENTRIES,
                f"{have.document_name} carries ledger entries and cannot be modified",
                [have],
            )
            return

        if wants_rename:
            self.renumbers.append(RenumberAccount(have, want))
        if wants_retype:
            self.retypes.append(RetypeAccount(have, want))

    def children(self, want: DesiredAccount, have: LiveAccount) -> None:
        """Match a matched pair's children to each other by account name.

        Only this pair's own children are candidates, which is what keeps a
        renamed parent from stranding its subtree. Ambiguity is therefore
        between siblings rather than chart-wide: two children of one parent
        sharing a name are indistinguishable, two accounts of the same name
        under different parents are not.
        """
        available: dict[str, list[LiveAccount]] = {}
        for child in self.live_children.get(have.document_name, []):
            available.setdefault(child.name, []).append(child)

        for kid in self.desired_children.get(want.number, []):
            candidates = available.get(kid.name, [])
            if len(candidates) > 1:
                found = ", ".join(c.document_name for c in candidates)
                self.refuse(
                    kid,
                    RefusalReason.AMBIGUOUS_MATCH,
                    f"{len(candidates)} children of {have.document_name} are named {kid.name!r}: {found}",
                    candidates,
                )
                self.consume(candidates)
            elif candidates:
                self.pair(kid, candidates[0])
            else:
                self.create(kid)

    def create(self, want: DesiredAccount) -> None:
        """Create an unmatched account and, with it, everything beneath it."""
        self.creates.append(CreateAccount(want))
        for child in self.desired_children.get(want.number, []):
            self.create(child)

    def consume(self, accounts: list[LiveAccount]) -> None:
        """Mark live subtrees as accounted for without matching them.

        A refused account must not also surface as an extra -- refused and
        unmentioned at once makes no sense -- and neither must its descendants,
        which the walk cannot reach now that it has stopped at their ancestor.
        """
        for account in accounts:
            for node in self._subtree(account):
                self.matched.add(node.document_name)

    def refuse(
        self,
        want: DesiredAccount,
        reason: RefusalReason,
        detail: str,
        accounts: list[LiveAccount],
    ) -> None:
        """Record a refusal against a desired account and the live accounts involved."""
        names = tuple(a.document_name for a in accounts)
        self.refusals.append(Refusal(self.keys[want.number], reason, detail, names))

    def _subtree(self, account: LiveAccount) -> Iterator[LiveAccount]:
        yield account
        for child in self.live_children.get(account.document_name, []):
            yield from self._subtree(child)


def _keys(desired: list[DesiredAccount]) -> dict[str, MatchKey]:
    """Label every desired account as (its name, its parent's name) for reporting."""
    by_number = {a.number: a for a in desired}
    keys: dict[str, MatchKey] = {}
    for account in desired:
        if account.parent_number is None:
            keys[account.number] = (account.name, None)
            continue
        parent = by_number.get(account.parent_number)
        if parent is None:
            raise KeyError(f"account {account.number} names parent {account.parent_number}, which is not in the chart")
        keys[account.number] = (account.name, parent.name)
    return keys


def _desired_children(desired: list[DesiredAccount]) -> dict[str | None, list[DesiredAccount]]:
    """Group desired accounts under their parent's number; roots under None."""
    children: dict[str | None, list[DesiredAccount]] = {}
    for account in desired:
        children.setdefault(account.parent_number, []).append(account)
    return children


def _live_children(live: list[LiveAccount]) -> dict[str | None, list[LiveAccount]]:
    """Group live accounts under their parent's document name; roots under None.

    A parent naming a document outside the given set is an error rather than a
    second root: the walk descends from the roots, so an account hanging off a
    missing parent would never be visited and would be reported as an extra --
    which reads as "the chart does not describe this" when the truth is "we
    never looked at it".
    """
    by_document_name = {a.document_name: a for a in live}
    children: dict[str | None, list[LiveAccount]] = {}
    for account in live:
        parent = account.parent_document_name
        if parent is not None and parent not in by_document_name:
            raise KeyError(f"account {account.document_name!r} names parent {parent!r}, which is not in the given set")
        children.setdefault(parent, []).append(account)
    return children
