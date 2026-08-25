"""Turn a ChartPlan into requests, in the only order that works.

Renumbers run before creates. A new account is linked to its parent by the
parent's *document name*, and a renumber changes that name -- so creating first
would link a child to a name that is about to stop existing. Renumbering first
means every parent's name is final before anything references it.

Renumbers among themselves need no ordering: an account's document name does
not contain its parent's, so renaming a parent updates its children's links
without changing their names.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from stolon.apply.diff import ChartPlan
from stolon.apply.model import DesiredAccount
from stolon.apply.order import order_creates
from stolon.erpnext.accounts import document_name

RENUMBER_METHOD = "erpnext.accounts.doctype.account.account.update_account_number"


class PlanRefusedError(RuntimeError):
    """The plan carried refusals, so nothing was executed.

    Raised before the first request. A partially applied chart is worse than an
    unapplied one: the next run diffs against a state nobody chose.
    """


class _Writer(Protocol):
    def insert(self, doctype: str, doc: dict[str, Any], /) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any], /) -> Any: ...
    def call(self, dotted_path: str, params: dict[str, Any] | None = None, /) -> Any: ...


@dataclasses.dataclass
class ApplyReport:
    """What actually happened, for printing back to the operator."""

    created: list[str] = dataclasses.field(default_factory=list)
    renumbered: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    retyped: list[str] = dataclasses.field(default_factory=list)


def apply_chart(
    client: _Writer,
    plan: ChartPlan,
    by_number: dict[str, DesiredAccount],
    abbr: str,
    company: str,
) -> ApplyReport:
    """Execute a plan. Raises PlanRefusedError before touching anything if it was refused."""
    if plan.refusals:
        reasons = ", ".join(sorted({r.reason.value for r in plan.refusals}))
        raise PlanRefusedError(f"plan carries {len(plan.refusals)} refusal(s): {reasons}")

    report = ApplyReport()

    # Track each desired account's live document name as it becomes final, so a
    # child created later links to the right parent.
    resolved: dict[str, str] = {}
    for change in plan.renumbers:
        new_name = client.call(
            RENUMBER_METHOD,
            {
                "name": change.live.document_name,
                "account_name": change.desired.name,
                "account_number": change.desired.number,
            },
        )
        new_name = str(new_name) if new_name else document_name(change.desired.name, abbr, change.desired.number)
        resolved[change.desired.number] = new_name
        report.renumbered.append((change.live.document_name, new_name))

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

    return report
