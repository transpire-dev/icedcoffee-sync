"""Delete accounts the chart does not describe.

This is the only operation in the package that destroys data, and the ledger
it runs against is real. Two design choices follow directly from that, and
both look inconsistent with `stolon.apply.diff` on purpose:

A `PruneRefusal` is per-account, not plan-wide. `ChartPlan` makes one
`Refusal` block the entire apply, because a partially applied chart leaves a
tree shape nobody chose for the next diff to reason about. Pruning has no
such hazard: an account this run declines to delete is simply an extra again
next time, so one undeletable account must not stop its siblings from being
cleaned up.

The guard here is narrow on purpose. `plan_prune` refuses only what it can
know locally -- ledger entries, roots, and unfinished child deletion -- and
leaves everything else to ERPNext itself. Reimplementing the server's
referential-integrity checks (what else links to this account) would be
guessing at rules better enforced by the system that owns them; a real
`LinkExistsError` from the delete call is the correct way to learn that.

That referential integrity turned out to include accounts ERPNext will not
delete at all -- wired into its own defaults or tax templates -- and says so:
"You can disable this Account instead of deleting it." `execute_prune` takes
that literally. A delete refused with `LinkExistsError`, `ValidationError`,
or `ChildExistsError` falls back to disabling the account rather than being
treated as a final failure: disabling is the retirement path ERPNext itself
supports, it is strictly less destructive than deleting, and reversible with
one field write, and every candidate has already passed `plan_prune`'s
checks. The fallback never inspects the delete error's message --
attempt-then-fallback needs no such match, and matching text would silently
stop working the day ERPNext rewords the string.

That exception set is deliberately narrow, not `except FrappeError`. All
three named classes mean the server refused on business grounds; anything
else -- `RateLimitError`, an unclassified 5xx -- is infrastructure trouble a
retry might resolve, and silently disabling an account because a request got
rate limited would be a much worse mistake than a noisy failure. Those
propagate out of `execute_prune` uncaught.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any, Protocol

from stolon.apply.model import LiveAccount
from stolon.frappe.errors import ChildExistsError, FrappeError, LinkExistsError, ValidationError


class PruneReason(enum.StrEnum):
    """Why an extra account was not put on the delete list."""

    HAS_GL_ENTRIES = "has_gl_entries"
    IS_ROOT = "is_root"
    HAS_LIVE_CHILD = "has_live_child"


@dataclasses.dataclass(frozen=True)
class DeleteAccount:
    """An extra account cleared to delete."""

    live: LiveAccount


@dataclasses.dataclass(frozen=True)
class PruneRefusal:
    """One extra account pruning will not touch, and why."""

    live: LiveAccount
    reason: PruneReason
    detail: str


@dataclasses.dataclass(frozen=True)
class PrunePlan:
    """What `plan_prune` concluded: deletes in execution order, plus refusals."""

    deletes: list[DeleteAccount]
    refusals: list[PruneRefusal]

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to prune and nothing to report."""
        return not (self.deletes or self.refusals)


def plan_prune(
    extras: list[LiveAccount],
    live: list[LiveAccount],
    accounts_with_entries: set[str],
) -> PrunePlan:
    """Decide which extras may be deleted, deepest first.

    An extra already disabled is skipped outright -- neither deleted nor
    refused, because a prior run (or a fallback in `execute_prune`) already
    retired it, and there is nothing left to decide. Retrying it forever on
    every subsequent run would be pure noise.

    Otherwise, an extra is refused rather than deleted when it carries
    ledger entries, when it is a root (ERPNext requires one root per
    root_type and rejects an Account with no parent, so a root can only
    ever be matched, never created or removed), or when it still has a live
    child that is not itself among the extras being deleted.

    That last case should not occur: under `stolon.apply.diff`'s tree
    matching, a matched account's parent is always matched too, so an
    extra's descendants are themselves all extras, and a whole extra subtree
    should come out deletable together. `HAS_LIVE_CHILD` exists to catch
    that invariant being wrong rather than to fire in normal operation.

    Args:
        extras: The live accounts the diff reported as not described by the
            chart -- the `.live` of each `stolon.apply.diff.ExtraAccount`.
        live: Every live account, extras and matched accounts alike -- needed
            to tell whether an extra's child is itself an extra or a
            surviving matched account.
        accounts_with_entries: Live document names carrying ledger entries, as
            of one probe taken at plan time. It is a snapshot, not a live
            check: an account that gains its first ledger entry between that
            probe and the operator's confirmation is cleared to delete here,
            then refused by ERPNext itself when `execute_prune` actually tries
            it (a `LinkExistsError`, since it now carries history), and falls
            back to disabling it instead -- reversible with one field write,
            and reported in the run's output as disabled rather than deleted.

    Returns:
        A plan whose deletes are ordered deepest first, so a parent is never
        asked to delete before its children.
    """
    extra_names = {account.document_name for account in extras}
    by_document_name = {account.document_name: account for account in live}

    live_children: dict[str, list[LiveAccount]] = {}
    for account in live:
        if account.parent_document_name is not None:
            live_children.setdefault(account.parent_document_name, []).append(account)

    deletable: list[LiveAccount] = []
    refusals: list[PruneRefusal] = []

    for account in extras:
        if account.disabled:
            continue

        if account.document_name in accounts_with_entries:
            refusals.append(
                PruneRefusal(
                    account,
                    PruneReason.HAS_GL_ENTRIES,
                    f"{account.document_name} carries ledger entries and cannot be deleted",
                )
            )
            continue

        if account.parent_document_name is None:
            refusals.append(
                PruneRefusal(
                    account,
                    PruneReason.IS_ROOT,
                    f"{account.document_name} is a root; ERPNext requires exactly one root per "
                    f"root_type and a root can only be matched, never created or deleted",
                )
            )
            continue

        surviving_children = [
            child.document_name
            for child in live_children.get(account.document_name, [])
            if child.document_name not in extra_names
        ]
        if surviving_children:
            found = ", ".join(surviving_children)
            refusals.append(
                PruneRefusal(
                    account,
                    PruneReason.HAS_LIVE_CHILD,
                    f"{account.document_name} still has a live child not being deleted: {found}",
                )
            )
            continue

        deletable.append(account)

    ordered = sorted(deletable, key=lambda account: _depth(account, by_document_name), reverse=True)
    return PrunePlan([DeleteAccount(account) for account in ordered], refusals)


def _depth(account: LiveAccount, by_document_name: dict[str, LiveAccount]) -> int:
    """How many ancestors an account has, walking parent_document_name.

    Guarded against a cycle: a cycle here would mean the live tree is
    corrupt in a way nothing upstream should produce, and a defensive guard
    that hangs is worse than one that gives up and returns whatever depth it
    had counted so far.
    """
    seen: set[str] = {account.document_name}
    steps = 0
    current = account
    while current.parent_document_name is not None:
        if current.parent_document_name in seen:
            break
        parent = by_document_name.get(current.parent_document_name)
        if parent is None:
            break
        seen.add(parent.document_name)
        current = parent
        steps += 1
    return steps


class _Client(Protocol):
    """The slice of Client a prune execution needs."""

    def delete(self, doctype: str, name: str, /) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any], /) -> Any: ...


@dataclasses.dataclass
class PruneReport:
    """What actually happened, for printing back to the operator."""

    deleted: list[str] = dataclasses.field(default_factory=list)
    disabled: list[str] = dataclasses.field(default_factory=list)
    failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)


# The `FrappeError` subclasses that mean ERPNext refused a delete on business
# grounds rather than infrastructure trouble: LinkExistsError is the "you can
# disable this Account instead" refusal itself, ValidationError covers the
# generic business-rule refusal a disabled child can also leave on its parent,
# and ChildExistsError (Frappe's NestedSetChildExistsError) is the "has child
# nodes" cascade specifically -- measured on a live site as its own error_type,
# neither a LinkExistsError nor a ValidationError, so it needs its own place in
# this tuple or that whole cascade path silently stops falling back to
# disable. Anything else -- RateLimitError, an unclassified 5xx -- is left to
# propagate, because a retry might clear it and silently disabling an account
# over a transient error would be a worse mistake than a noisy failure.
_DISABLE_ELIGIBLE: tuple[type[FrappeError], ...] = (LinkExistsError, ValidationError, ChildExistsError)


def execute_prune(client: _Client, plan: PrunePlan) -> PruneReport:
    """Delete each planned account in order, falling back to disable, continuing past a failure.

    A delete failing with `LinkExistsError`, `ValidationError`, or
    `ChildExistsError` is caught per account and answered with a second
    attempt: disable the account instead. That fallback runs on those
    classes only, never on a match against the error's text --
    attempt-then-fallback needs no such match, and matching text would
    silently stop working the day ERPNext rewords the string. Any other
    `FrappeError` (`RateLimitError`, an unclassified 5xx) propagates out of
    this call uncaught: it signals infrastructure trouble a retry might
    resolve, not a business refusal, and disabling an account over that
    would be the wrong call.

    Only when the disable attempt *also* raises one of those same three
    classes is the account recorded as failed, carrying that second error's
    message -- the delete's own message is superseded, not lost information
    worth keeping, since the fallback conclusively answers whether the
    account could be retired at all. A disable failing with anything else
    propagates uncaught too, symmetrically with the delete: infrastructure
    trouble is infrastructure trouble regardless of which of the two calls
    hit it.

    Neither a delete failure eligible for fallback nor a disable failure
    on business grounds ends the run: one unresolvable account is the same
    shape of problem as a refusal, and should not stop the rest from being
    cleared.

    Args:
        client: Anything that can delete or update an Account document.
        plan: The plan from `plan_prune`. Deletes run in the given order,
            which is deepest first.

    Returns:
        What was deleted, what was disabled instead, and what failed both
        ways along with the message from the second `FrappeError`.

    Raises:
        FrappeError: A delete or the disable fallback it triggered failed
            with something other than `LinkExistsError`, `ValidationError`,
            or `ChildExistsError`.
    """
    report = PruneReport()
    for change in plan.deletes:
        name = change.live.document_name
        try:
            client.delete("Account", name)
        except _DISABLE_ELIGIBLE:
            _fall_back_to_disable(client, name, report)
        else:
            report.deleted.append(name)
    return report


def _fall_back_to_disable(client: _Client, name: str, report: PruneReport) -> None:
    """Disable an account whose delete just failed, recording the outcome.

    Catches only `_DISABLE_ELIGIBLE` -- the same tuple the delete attempt
    checks -- so this stays symmetric with it: those classes mean the server
    refused the disable on business grounds too, and the account is recorded
    as failed to retire either way. Anything else, `RateLimitError` chief
    among them, propagates out of `execute_prune` uncaught rather than being
    recorded as a business failure; disabling was already the fallback for a
    business refusal, and treating infrastructure trouble as one more silently
    accepts a wrong outcome that a retry might have avoided.
    """
    try:
        client.update("Account", name, {"disabled": 1})
    except _DISABLE_ELIGIBLE as error:
        report.failed.append((name, str(error)))
    else:
        report.disabled.append(name)
