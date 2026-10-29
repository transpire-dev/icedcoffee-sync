"""Order changes so each one can actually be executed when its turn comes.

ERPNext links a new account to its parent's *document name*, so the parent
must exist first: creates run shallowest-first, and every renumber runs
before any create. Renumbers also need their own order among themselves --
measured live, "Current Assets" taking 1100 before "Cash In Hand" vacated it
died with "number already used", a collision `ChartPlan` deliberately leaves
for `order_renames` to resolve rather than refusing outright.
"""

from __future__ import annotations

import dataclasses

from icedcoffee_sync.apply.diff import CreateAccount, RenameAccount
from icedcoffee_sync.apply.model import DesiredAccount, LiveAccount
from icedcoffee_sync.apply.park import _next_candidate


class CyclicChartError(ValueError):
    """A chart whose parent links form a loop."""


def depth(account: DesiredAccount, by_number: dict[str, DesiredAccount]) -> int:
    """How many ancestors an account has. Raises on a cycle or dangling parent."""
    seen: list[str] = [account.number]
    steps = 0
    current = account
    while current.parent_number is not None:
        if current.parent_number in seen:
            raise CyclicChartError(f"parent cycle through {' -> '.join([*seen, current.parent_number])}")
        seen.append(current.parent_number)
        parent = by_number.get(current.parent_number)
        if parent is None:
            raise KeyError(f"account {current.number} names parent {current.parent_number}, which is not in the chart")
        current = parent
        steps += 1
    return steps


def order_creates(creates: list[CreateAccount], by_number: dict[str, DesiredAccount]) -> list[CreateAccount]:
    """Shallowest first, preserving chart order within a depth -- a stable sort, since depth is a valid tree order."""
    return sorted(creates, key=lambda c: depth(c.desired, by_number))


@dataclasses.dataclass(frozen=True)
class ParkStep:
    """A temporary renumber, not a chart member, that frees `live`'s number for whoever is waiting on it."""

    live: LiveAccount
    temporary_number: str


type RenameStep = RenameAccount | ParkStep


def order_renames(
    renames: list[RenameAccount], chart_numbers: frozenset[str], taken: frozenset[str]
) -> list[RenameStep]:
    """Order renames so none tries to claim a number still occupied by another.

    Renumber X depends on renumber Y when `X.desired.number ==
    Y.live.number`; `ChartPlan` deliberately leaves that unrefused, so a
    topological sort must resolve it, breaking a genuine cycle by parking
    its lowest-indexed member onto a temporary number.

    Args:
        chart_numbers: Every account number the chart describes.
        taken: Every number occupied on the live site, including accounts
            this plan does not touch.

    Returns:
        `renames`, reordered, with a `ParkStep` wherever a cycle was broken.
    """
    n = len(renames)
    dependents, remaining = _dependency_graph(renames)
    avoid = taken | chart_numbers

    done: set[int] = set()
    vacated: set[int] = set()
    parked_at: set[str] = set()
    steps: list[RenameStep] = []

    def _next_ready() -> int | None:
        return next((i for i in range(n) if i not in done and remaining[i] == 0), None)

    def _vacate(i: int) -> None:
        vacated.add(i)
        for dependent in dependents[i]:
            remaining[dependent] -= 1

    while len(done) < n:
        ready = _next_ready()
        if ready is not None:
            steps.append(renames[ready])
            done.add(ready)
            _vacate(ready)
            continue

        # A cycle: park the lowest-indexed unvacated member to free its number.
        to_park = next(i for i in range(n) if i not in vacated)
        candidate = _unused_number(renames[to_park].live.number, avoid | parked_at)
        parked_at.add(candidate)
        steps.append(ParkStep(renames[to_park].live, candidate))
        _vacate(to_park)

    return steps


def _dependency_graph(renames: list[RenameAccount]) -> tuple[list[set[int]], list[int]]:
    """Which renames must run before which, and how many each is still waiting on."""
    n = len(renames)
    depends_on = [
        {j for j in range(n) if j != i and renames[i].desired.number == renames[j].live.number} for i in range(n)
    ]
    dependents: list[set[int]] = [set() for _ in range(n)]
    for i, deps in enumerate(depends_on):
        for j in deps:
            dependents[j].add(i)
    remaining = [len(deps) for deps in depends_on]
    return dependents, remaining


def _unused_number(number: str, avoid: frozenset[str] | set[str]) -> str:
    """`number`, escalated with `_next_candidate` until it names nothing in `avoid`."""
    candidate = _next_candidate(number)
    while candidate in avoid:
        candidate = _next_candidate(candidate)
    return candidate
