"""Compare a desired chart against a live one and say what would change.

Accounts have no intrinsic key: identity comes from position, walked
top-down. A root is matched by root_type, since ERPNext rejects an Account
with no `parent_account` and so roots are never created. A child is matched
against its parent's already-resolved *live* identity, not a pre-apply
index, so a renamed parent's children still match.

Extras (accounts the chart doesn't describe) are reported and left alone --
deletion is opt-in. A single refusal makes the whole plan unapplyable, since
a partially applied chart leaves a tree shape nobody chose to diff against.
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
    NUMBER_COLLISION = "number_collision"


@dataclasses.dataclass(frozen=True)
class CreateAccount:
    """An account in the chart with no counterpart on the site."""

    desired: DesiredAccount


@dataclasses.dataclass(frozen=True)
class RenameAccount:
    """A matched account whose number or name differs.

    One bucket for both because ERPNext names a document
    `{number} - {name} - {abbr}`: changing either field is the same single
    rename call, and ordering them is the same problem either way.

    They are not the same thing to a person, though, so `changes_number` and
    `changes_name` tell them apart. A renumber moves the GL code the bank
    shows against every transaction; a rename only changes the label on it.
    """

    live: LiveAccount
    desired: DesiredAccount

    @property
    def changes_number(self) -> bool:
        """True when the account's number moves."""
        return self.live.number != self.desired.number

    @property
    def changes_name(self) -> bool:
        """True when the account's name changes."""
        return self.live.name != self.desired.name


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
class EnableAccount:
    """A matched account the chart wants enabled, but the site has disabled.

    The chart format has no field for "disabled", so any matched account is
    implicitly wanted enabled -- `disabled=True` is always drift. Never
    checked against `with_entries`: re-enabling touches neither GL entries
    nor the document's identity.
    """

    live: LiveAccount
    desired: DesiredAccount


@dataclasses.dataclass(frozen=True)
class Refusal:
    """A situation the diff will not resolve on its own.

    `live_document_names` holds the live accounts the refusal is about -- one
    for a mismatch, several for an ambiguous match, none when the chart wants
    an account the site doesn't have.
    """

    key: MatchKey
    reason: RefusalReason
    detail: str
    live_document_names: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class ChartPlan:
    """Everything the diff concluded.

    `enables` defaults to empty and sits last so positional `ChartPlan(...)`
    construction (test fixtures, especially) needs no updating.
    """

    creates: list[CreateAccount]
    renames: list[RenameAccount]
    retypes: list[RetypeAccount]
    extras: list[ExtraAccount]
    refusals: list[Refusal]
    enables: list[EnableAccount] = dataclasses.field(default_factory=list)

    @property
    def is_applyable(self) -> bool:
        """True when nothing was refused. Extras do not block."""
        return not self.refusals

    @property
    def is_empty(self) -> bool:
        """True when the site already matches the chart."""
        return not (self.creates or self.renames or self.retypes or self.enables)


def diff(
    desired: list[DesiredAccount],
    live: list[LiveAccount],
    accounts_with_entries: set[str],
) -> ChartPlan:
    """Diff a desired chart against a live one.

    Args:
        desired: The chart as the file describes it.
        live: Every account the site has for the company.
        accounts_with_entries: Live document names carrying ledger entries.
            Only accounts this diff would otherwise *change* are checked
            against it.

    Returns:
        Everything the walk concluded, refusals included.

    Raises:
        KeyError: A desired or live account names a parent outside its set.
        ValueError: The chart declares two roots of the same root_type.
    """
    walk = _Walk(
        with_entries=accounts_with_entries,
        keys=_keys(desired),
        desired_children=_desired_children(desired),
        live_children=_live_children(live),
    )
    walk.roots()
    walk.number_collisions(live)
    extras = [ExtraAccount(a) for a in live if a.document_name not in walk.matched]
    return ChartPlan(walk.creates, walk.renames, walk.retypes, extras, walk.refusals, walk.enables)


@dataclasses.dataclass
class _Walk:
    """One diff in progress: both trees, and what the walk has concluded so far."""

    with_entries: set[str]
    keys: dict[str, MatchKey]
    desired_children: dict[str | None, list[DesiredAccount]]
    live_children: dict[str | None, list[LiveAccount]]
    creates: list[CreateAccount] = dataclasses.field(default_factory=list)
    renames: list[RenameAccount] = dataclasses.field(default_factory=list)
    retypes: list[RetypeAccount] = dataclasses.field(default_factory=list)
    enables: list[EnableAccount] = dataclasses.field(default_factory=list)
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

        Children are walked even for a refused pair: nothing runs from a
        refused plan, so a create proposed under a disputed parent is just
        information.
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
            self.renames.append(RenameAccount(have, want))
        if wants_retype:
            self.retypes.append(RetypeAccount(have, want))
        if have.disabled:
            self.enables.append(EnableAccount(have, want))

    def children(self, want: DesiredAccount, have: LiveAccount) -> None:
        """Match a matched pair's children to each other, by number and then by name.

        Only this pair's own children are candidates -- keeps a renamed
        parent from stranding its subtree, and makes ambiguity a
        sibling-only concern rather than a chart-wide one.

        **Number first.** In a chart of accounts the number is the account's
        identity and the name is a label on it: the chart file is keyed on
        number, ERPNext enforces one account per number per company, and the
        GL codes our bank shows are those numbers. So an account whose name
        the chart rewrites is the same account, and matching it by name would
        make it a create beside the original -- which then collides with the
        number the original still holds, and the plan refuses the very change
        it was asked to make.

        **Name second**, for the live accounts a number cannot reach: the
        stock chart a wizard site starts with carries no numbers at all, so
        number-only matching would strand every one of them and re-create the
        chart alongside itself.

        Two passes rather than one loop, so a name cannot claim a sibling
        that some later chart entry owns by number. A number match is never
        ambiguous the way a name match is, which is also why a number match
        settles what would otherwise be two same-named siblings.
        """
        siblings = self.live_children.get(have.document_name, [])
        kids = self.desired_children.get(want.number, [])

        counterparts: list[LiveAccount | None] = [None] * len(kids)
        taken: set[str] = set()
        self._claim_by_number(kids, siblings, counterparts, taken)
        refused = self._claim_by_name(kids, siblings, counterparts, taken, have)

        for index, kid in enumerate(kids):
            if index in refused:
                continue
            counterpart = counterparts[index]
            if counterpart is None:
                self.create(kid)
            else:
                self.pair(kid, counterpart)

    @staticmethod
    def _claim_by_number(
        kids: list[DesiredAccount],
        siblings: list[LiveAccount],
        counterparts: list[LiveAccount | None],
        taken: set[str],
    ) -> None:
        """Pair each chart entry with the sibling already holding its number."""
        by_number = {child.number: child for child in siblings if child.number}
        for index, kid in enumerate(kids):
            match = by_number.get(kid.number)
            if match is not None:
                counterparts[index] = match
                taken.add(match.document_name)

    def _claim_by_name(
        self,
        kids: list[DesiredAccount],
        siblings: list[LiveAccount],
        counterparts: list[LiveAccount | None],
        taken: set[str],
        have: LiveAccount,
    ) -> set[int]:
        """Pair what the numbers left over, by name. Returns the indices refused as ambiguous."""
        by_name: dict[str, list[LiveAccount]] = {}
        for child in siblings:
            by_name.setdefault(child.name, []).append(child)

        refused: set[int] = set()
        for index, kid in enumerate(kids):
            if counterparts[index] is not None:
                continue
            candidates = [c for c in by_name.get(kid.name, []) if c.document_name not in taken]
            if len(candidates) > 1:
                found = ", ".join(c.document_name for c in candidates)
                self.refuse(
                    kid,
                    RefusalReason.AMBIGUOUS_MATCH,
                    f"{len(candidates)} children of {have.document_name} are named {kid.name!r}: {found}",
                    candidates,
                )
                self.consume(candidates)
                taken.update(c.document_name for c in candidates)
                refused.add(index)
            elif candidates:
                counterparts[index] = candidates[0]
                taken.add(candidates[0].document_name)
        return refused

    def number_collisions(self, live: list[LiveAccount]) -> None:
        """Refuse any create or renumber that would land on a number a live account still holds.

        Not local to a matched pair, unlike `reconcile`'s checks: the
        contested account can be anywhere in the tree. Checked against the
        state *after* this plan's own renames land -- `apply_chart` runs
        renames before creates, so a number this plan itself vacates is
        not a collision, or a valid renumbering plan would refuse itself
        forever.

        Args:
            live: Every account the site has for the company -- the
                contested account may be one this walk never visited.
        """
        live_by_number = {a.number: a for a in live if a.number}
        vacated = {r.live.number for r in self.renames if r.live.number and r.live.number != r.desired.number}

        def check(want: DesiredAccount, counterpart: LiveAccount | None) -> None:
            if not want.number or want.number in vacated:
                return
            contested = live_by_number.get(want.number)
            if contested is None:
                return
            if counterpart is not None and contested.document_name == counterpart.document_name:
                return
            self.refuse(
                want,
                RefusalReason.NUMBER_COLLISION,
                f"chart assigns {want.number} to {want.name!r} but {want.number} is held by "
                f"{contested.document_name!r}",
                [contested],
            )

        for change in self.creates:
            check(change.desired, None)
        for change in self.renames:
            check(change.desired, change.live)

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

    Raises `KeyError` if a parent names a document outside the given set:
    the walk descends only from roots, so such an account would otherwise be
    silently misreported as an extra rather than one nobody looked at.
    """
    by_document_name = {a.document_name: a for a in live}
    children: dict[str | None, list[LiveAccount]] = {}
    for account in live:
        parent = account.parent_document_name
        if parent is not None and parent not in by_document_name:
            raise KeyError(f"account {account.document_name!r} names parent {parent!r}, which is not in the given set")
        children.setdefault(parent, []).append(account)
    return children
