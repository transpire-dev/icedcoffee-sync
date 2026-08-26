"""Move a survivor's account number out of the chart's way, without deleting it.

`--allow-unapplied` prune can leave live accounts behind that `plan_prune`
never offered to delete, or that `execute_prune` could only disable: ERPNext
itself keeps some of its sample accounts alive by refilling certain Company
defaults BY ACCOUNT NAME on every save (`default_income_account`,
`write_off_account`, `exchange_gain_loss_account`, `disposal_account` --
measured against `erpnext/setup/doctype/company/company.py`'s `on_update`),
so those specific accounts can never be retired before `coa apply` has
pointed the defaults at our own accounts instead. When such a survivor's
account number is one the chart itself needs, `coa apply` cannot create the
chart's account at that number either -- a `NUMBER_COLLISION` refusal, a
second cycle. Parking breaks it: renumber the survivor onto an unused number
high enough to never collide, so `coa apply` can create the real account,
point the defaults at it, and a closing `coa prune` retire the parked
survivor for good, now that nothing needs it by name any more.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from stolon.apply.model import LiveAccount

# The same whitelisted method `stolon.apply.execute.apply_chart` uses to
# renumber a chart account -- reused here so a survivor's rename goes through
# the identical server-side path apply already relies on.
RENUMBER_METHOD = "erpnext.accounts.doctype.account.account.update_account_number"


@dataclasses.dataclass(frozen=True)
class ParkedNumber:
    """One survivor renumbered out of the chart's way."""

    old_document_name: str
    number: str
    parked_number: str
    new_document_name: str


class _Client(Protocol):
    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any: ...


def _next_candidate(number: str) -> str:
    """One more `9` prepended -- the escalation `park_colliding_numbers` retries with."""
    return f"9{number}"


def park_colliding_numbers(
    client: _Client,
    survivors: list[LiveAccount],
    chart_numbers: frozenset[str],
    taken: frozenset[str],
) -> list[ParkedNumber]:
    """Renumber every survivor whose number the chart still wants.

    A survivor with no parent is a root and is skipped outright: `coa apply`
    matches roots by `root_type`, not by number, and renames them itself --
    parking one here would just be a redundant, conflicting rename.

    For every other survivor whose `number` is in `chart_numbers`, the parked
    number is `9` followed by the original -- prepending another `9` for as
    long as the candidate collides with `taken`, `chart_numbers`, or a number
    this call already chose for an earlier survivor. `account_name` is sent
    unchanged; only the number moves.

    Args:
        client: Anything that can call a whitelisted server method.
        survivors: Live accounts still on the site after prune -- disabled,
            already-disabled, failed, or refused outright by `plan_prune`.
        chart_numbers: Every account number the chart describes.
        taken: Every account number already occupied on the site (plus,
            typically, `chart_numbers` itself -- the caller's choice of how
            widely to check).

    Returns:
        One `ParkedNumber` per survivor actually renumbered, in the order
        `survivors` was given.
    """
    parked: list[ParkedNumber] = []
    chosen: set[str] = set()
    for survivor in survivors:
        if survivor.parent_document_name is None:
            continue
        if survivor.number not in chart_numbers:
            continue

        candidate = _next_candidate(survivor.number)
        while candidate in taken or candidate in chart_numbers or candidate in chosen:
            candidate = _next_candidate(candidate)
        chosen.add(candidate)

        new_name = client.call(
            RENUMBER_METHOD,
            {
                "name": survivor.document_name,
                "account_name": survivor.name,
                "account_number": candidate,
            },
        )
        # Same fallback shape as `apply_chart`'s renumber step: the document
        # name is "{number} - {name} - {abbr}", and only the number changed,
        # so replacing just that leading prefix reproduces it without needing
        # the abbreviation as a separate input.
        new_name = (
            str(new_name) if new_name else survivor.document_name.replace(f"{survivor.number} - ", f"{candidate} - ", 1)
        )
        parked.append(ParkedNumber(survivor.document_name, survivor.number, candidate, new_name))
    return parked
