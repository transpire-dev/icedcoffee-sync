"""Move a surviving account's number out of the chart's way, without deleting it.

ERPNext refills four Company defaults (income, write-off, exchange gain/loss,
asset disposal) by account name on every Company save and refuses to retire
a default, so some pre-existing accounts cannot go before apply has pointed
the defaults elsewhere -- yet apply cannot create the chart's account on a
number they hold. Parking renumbers the survivor (9-prefixed) so apply can
run; the closing prune retires it afterwards.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from icedcoffee_sync.apply.model import LiveAccount
from icedcoffee_sync.apply.prune import PruneReason, PruneRefusal, PruneReport

# The whitelisted rename apply itself uses; a field update does not move the document.
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
    """Renumber every non-root survivor holding a chart number onto a free 9-prefixed one.

    Roots are skipped: apply matches them by root type and renames them
    itself. `taken` is every number occupied on the site; the chosen number
    also avoids `chart_numbers` and earlier picks. Only the number moves.
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
        # Document names are "{number} - {name} - {abbr}"; only the number changed.
        new_name = (
            str(new_name) if new_name else survivor.document_name.replace(f"{survivor.number} - ", f"{candidate} - ", 1)
        )
        parked.append(ParkedNumber(survivor.document_name, survivor.number, candidate, new_name))
    return parked


def parking_candidates(
    extras: list[LiveAccount],
    report: PruneReport,
    refusals: list[PruneRefusal],
) -> tuple[list[LiveAccount], list[LiveAccount]]:
    """Split prune's survivors into `(candidates, gl_excluded)`.

    Candidates are the extras the run could not retire plus those refused
    for any reason but GL entries -- renumbering never touches ledger
    history, so a live child or root status is no obstacle. Extras refused
    for GL entries are returned separately so the caller can still report
    them as unresolved collisions.
    """
    gl_excluded = [refusal.live for refusal in refusals if refusal.reason is PruneReason.HAS_GL_ENTRIES]
    survivor_names = set(report.disabled) | set(report.already_disabled) | {name for name, _ in report.failed}
    survivor_names |= {
        refusal.live.document_name for refusal in refusals if refusal.reason is not PruneReason.HAS_GL_ENTRIES
    }
    candidates = [extra for extra in extras if extra.document_name in survivor_names]
    return candidates, gl_excluded


def unresolved_collisions(excluded: list[LiveAccount], chart_numbers: frozenset[str]) -> list[str]:
    """Document names of the non-root `excluded` accounts still holding a chart number -- a failure to report."""
    return [a.document_name for a in excluded if a.parent_document_name is not None and a.number in chart_numbers]
