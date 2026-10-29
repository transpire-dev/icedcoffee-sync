"""Delete accounts the chart does not describe.

The only operation here that destroys data, against a real ledger. A
`PruneRefusal` is per-account, not plan-wide like `diff`'s `Refusal`: a
declined account is just an extra again next time and must not block its
siblings.

`plan_prune` refuses only what it knows locally, leaving referential
integrity to ERPNext's own `LinkExistsError`. Some accounts ERPNext names
disable as the only alternative; `execute_prune` falls back to disable on
that error, `ValidationError`, or `ChildExistsError` only.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any, Protocol

from icedcoffee_sync.apply.model import LiveAccount
from icedcoffee_sync.frappe.errors import ChildExistsError, FrappeError, LinkExistsError, ValidationError


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

    An extra already disabled is re-attempted, since whatever blocked it
    might not still apply. Refused rather than deleted when it carries
    ledger entries (per `accounts_with_entries`, a snapshot from plan time),
    is a root (only ever matched, never created or removed), or still has a
    live child -- checked against which children actually cleared, not mere
    presence in `extras`, which is why extras are decided deepest first.
    """
    by_document_name = {account.document_name: account for account in live}

    live_children: dict[str, list[LiveAccount]] = {}
    for account in live:
        if account.parent_document_name is not None:
            live_children.setdefault(account.parent_document_name, []).append(account)

    deletable: list[LiveAccount] = []
    deletable_names: set[str] = set()
    refusals: list[PruneRefusal] = []

    # Deepest first: a parent's eligibility depends on its own extra children.
    ordered_extras = sorted(extras, key=lambda account: _depth(account, by_document_name), reverse=True)

    for account in ordered_extras:
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

        # A child survives unless it already cleared to delete -- checked
        # against `deletable_names`, not mere presence in `extras`.
        surviving_children = [
            child.document_name
            for child in live_children.get(account.document_name, [])
            if child.document_name not in deletable_names
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
        deletable_names.add(account.document_name)

    ordered = sorted(deletable, key=lambda account: _depth(account, by_document_name), reverse=True)
    return PrunePlan([DeleteAccount(account) for account in ordered], refusals)


def _depth(account: LiveAccount, by_document_name: dict[str, LiveAccount]) -> int:
    """How many ancestors an account has. Gives up on a cycle rather than hanging."""
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
    """What happened. `disabled` is a fresh fallback disable; `already_disabled` came in disabled and stayed refused."""

    deleted: list[str] = dataclasses.field(default_factory=list)
    disabled: list[str] = dataclasses.field(default_factory=list)
    already_disabled: list[str] = dataclasses.field(default_factory=list)
    failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)


# FrappeError subclasses meaning ERPNext refused a delete on business
# grounds; anything else (RateLimitError, an unclassified 5xx) is
# infrastructure trouble that propagates instead.
_DISABLE_ELIGIBLE: tuple[type[FrappeError], ...] = (LinkExistsError, ValidationError, ChildExistsError)


def execute_prune(client: _Client, plan: PrunePlan) -> PruneReport:
    """Delete each planned account in order, falling back to disable, continuing past a failure.

    A delete refused with a `_DISABLE_ELIGIBLE` class falls back to
    disabling instead; a disable failing the same way is recorded `failed`.
    Neither ends the run. An account already disabled coming in is still
    attempted, and if refused again is reported `already_disabled` with no
    redundant disable.

    Raises:
        FrappeError: A delete or its disable fallback failed with something
            outside `_DISABLE_ELIGIBLE`.
    """
    report = PruneReport()
    for change in plan.deletes:
        name = change.live.document_name
        try:
            client.delete("Account", name)
        except _DISABLE_ELIGIBLE:
            if change.live.disabled:
                report.already_disabled.append(name)
            else:
                _fall_back_to_disable(client, name, report)
        else:
            report.deleted.append(name)
    return report


def _fall_back_to_disable(client: _Client, name: str, report: PruneReport) -> None:
    """Disable an account whose delete just failed, recording the outcome.

    Catches only `_DISABLE_ELIGIBLE`, symmetric with the delete attempt;
    anything else propagates uncaught the same way.
    """
    try:
        client.update("Account", name, {"disabled": 1})
    except _DISABLE_ELIGIBLE as error:
        report.failed.append((name, str(error)))
    else:
        report.disabled.append(name)
