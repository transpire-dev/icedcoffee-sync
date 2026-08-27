"""Turn a ChartPlan into requests, in the only order that works.

Renumbers run before creates: a new account links to its parent by the
parent's *document name*, and a renumber changes that name, so creating
first would link a child to a name about to stop existing.

Renumbers need their own order among themselves too -- measured live,
renumbering "Current Assets" to 1100 before "Cash In Hand" (still sitting on
1100) vacated it died with a plain "number already used" error.
`stolon.apply.order.order_renumbers` works out that order, inserting a park
step onto a temporary number when two or more renumbers form a genuine cycle.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from stolon.apply.diff import ChartPlan, RenumberAccount
from stolon.apply.model import DesiredAccount, LiveAccount
from stolon.apply.order import ParkStep, order_creates, order_renumbers
from stolon.erpnext.accounts import document_name

RENUMBER_METHOD = "erpnext.accounts.doctype.account.account.update_account_number"


class PlanRefusedError(RuntimeError):
    """The plan carried refusals; raised before the first request so nothing runs partially applied."""


class _Writer(Protocol):
    def insert(self, doctype: str, doc: dict[str, Any], /) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any], /) -> Any: ...
    def call(self, dotted_path: str, params: dict[str, Any] | None = None, /) -> Any: ...


@dataclasses.dataclass
class ApplyReport:
    """What actually happened, for printing back to the operator.

    `parked` is every account `order_renumbers` moved out of the way to
    break a renumbering cycle -- distinct from `renumbered`, the moves the
    chart wanted. A parked account's real renumber still lands in `renumbered` too.
    """

    created: list[str] = dataclasses.field(default_factory=list)
    renumbered: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    parked: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)
    retyped: list[str] = dataclasses.field(default_factory=list)
    enabled: list[str] = dataclasses.field(default_factory=list)


def _execute_renumbers(
    client: _Writer,
    renumbers: list[RenumberAccount],
    abbr: str,
    report: ApplyReport,
    chart_numbers: frozenset[str],
    taken: frozenset[str],
) -> dict[str, str]:
    """Run every renumber, and any park step `order_renumbers` had to insert, in order.

    A parked account's document name changes twice -- to its temporary
    number, then its real one -- so `current_name` tracks each renumbered
    account's *current* document name (keyed by its original one) rather
    than trusting `RenumberAccount.live.document_name`, which only ever
    holds the pre-run name.

    Returns:
        The same desired-number -> final-document-name map `apply_chart`
        threads through creates and retypes.
    """
    resolved: dict[str, str] = {}
    current_name: dict[str, str] = {}

    def name_of(live: LiveAccount) -> str:
        return current_name.get(live.document_name, live.document_name)

    for step in order_renumbers(renumbers, chart_numbers, taken):
        if isinstance(step, ParkStep):
            new_name = client.call(
                RENUMBER_METHOD,
                {
                    "name": name_of(step.live),
                    "account_name": step.live.name,
                    "account_number": step.temporary_number,
                },
            )
            new_name = (
                str(new_name)
                if new_name
                else _swap_leading_number(step.live.document_name, step.live.number, step.temporary_number)
            )
            current_name[step.live.document_name] = new_name
            report.parked.append((step.live.document_name, step.temporary_number, new_name))
            continue

        new_name = client.call(
            RENUMBER_METHOD,
            {
                "name": name_of(step.live),
                "account_name": step.desired.name,
                "account_number": step.desired.number,
            },
        )
        new_name = str(new_name) if new_name else document_name(step.desired.name, abbr, step.desired.number)
        resolved[step.desired.number] = new_name
        report.renumbered.append((step.live.document_name, new_name))

    return resolved


def _swap_leading_number(old_document_name: str, old_number: str, new_number: str) -> str:
    """Fallback when the server returns no name: swap just the leading number.

    A document name is `"{number} - {name} - {abbr}"`; only the number
    changed, so replacing that leading prefix reproduces it without the
    abbreviation as a separate input (mirrors `stolon.apply.park`'s
    identical fallback).
    """
    return old_document_name.replace(f"{old_number} - ", f"{new_number} - ", 1)


def apply_chart(
    client: _Writer,
    plan: ChartPlan,
    live: list[LiveAccount],
    by_number: dict[str, DesiredAccount],
    abbr: str,
    company: str,
) -> ApplyReport:
    """Execute a plan. Raises PlanRefusedError before touching anything if it was refused.

    `live` is the whole pre-diff live chart, not just the accounts `plan`
    singles out: an account `plan` never mentions can still already hold a
    number `order_renumbers` might otherwise pick to park something onto.
    """
    if plan.refusals:
        reasons = ", ".join(sorted({r.reason.value for r in plan.refusals}))
        raise PlanRefusedError(f"plan carries {len(plan.refusals)} refusal(s): {reasons}")

    chart_numbers = frozenset(by_number)
    taken = frozenset(a.number for a in live if a.number)
    report = ApplyReport()
    resolved = _execute_renumbers(client, plan.renumbers, abbr, report, chart_numbers, taken)

    for change in order_creates(plan.creates, by_number):
        want = change.desired
        parent_name: str | None = None
        if want.parent_number is not None:
            parent = by_number[want.parent_number]
            parent_name = resolved.get(want.parent_number, document_name(parent.name, abbr, parent.number))
        doc: dict[str, Any] = {
            "account_name": want.name,
            "account_number": want.number,
            "company": company,
            "is_group": 1 if want.is_group else 0,
            "root_type": want.root_type,
        }
        if parent_name is not None:
            doc["parent_account"] = parent_name
        if want.account_type:
            doc["account_type"] = want.account_type
        created = client.insert("Account", doc)
        created_name = created.get("name") if isinstance(created, dict) else None
        created_name = created_name or document_name(want.name, abbr, want.number)
        resolved[want.number] = created_name
        report.created.append(created_name)

    for change in plan.retypes:
        target = resolved.get(change.desired.number, change.live.document_name)
        client.update("Account", target, {"account_type": change.desired.account_type or ""})
        report.retyped.append(target)

    for change in plan.enables:
        target = resolved.get(change.desired.number, change.live.document_name)
        client.update("Account", target, {"disabled": 0})
        report.enabled.append(target)

    return report
