"""Order changes so each one can actually be executed when its turn comes.

Creates need ordering among themselves: ERPNext links a new account to its
parent's *document name*, so the parent must exist first. That is why the
executor runs every renumber before any create: an account's own document
name does not contain its parent's, so renaming a parent updates its
children's links without changing their names, and by the time a child is
created, its parent's document name is final regardless of what order the
renumbers ran in.

Renumbers need ordering among themselves too, though -- measured live:
"Current Assets" tried to take account number 1100 while "Cash In Hand" was
still sitting on it, and ERPNext refused with a plain "number already used"
error. `stolon.apply.diff.ChartPlan` deliberately does not refuse this shape
of collision -- a number another renumber in the same plan is about to
vacate is not treated as contested -- so `apply_chart` has to actually run
that vacating renumber first, not just assume the diff already ruled it out.
"""

from __future__ import annotations

import dataclasses

from stolon.apply.diff import CreateAccount, RenumberAccount
from stolon.apply.model import DesiredAccount, LiveAccount
from stolon.apply.park import _next_candidate


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
    """Shallowest first, preserving chart order within a depth.

    A stable sort rather than a full topological walk: depth is a valid
    topological order for a tree, and keeping chart order inside a level makes
    the printed plan read like the chart file.
    """
    return sorted(creates, key=lambda c: depth(c.desired, by_number))


@dataclasses.dataclass(frozen=True)
class ParkStep:
    """A temporary renumber inserted to break a cycle among renumbers.

    Not a member of the chart -- nothing desires this number. It exists only
    to free `live`'s original number for whoever is waiting on it; `live`'s
    own real renumber (a `RenumberAccount`, further along in the same
    ordered list) is what actually reaches the chart's intended number.
    """

    live: LiveAccount
    temporary_number: str


type RenumberStep = RenumberAccount | ParkStep


def order_renumbers(renumbers: list[RenumberAccount]) -> list[RenumberStep]:
    """Order renumbers so none tries to claim a number still occupied by another.

    Renumber X depends on renumber Y when `X.desired.number ==
    Y.live.number` -- X wants the number Y currently sits on, so Y must run
    (and thereby vacate it) first. This is a real dependency `ChartPlan`'s
    own collision check deliberately does not refuse (a number another
    renumber in the plan is about to vacate is not treated as contested,
    precisely so a valid renumbering plan does not refuse itself forever) --
    which means nothing upstream of this function guarantees the dependency
    is even resolvable by ordering alone.

    It usually is: a plain topological sort (Kahn's algorithm, always
    picking the lowest original index among the accounts currently free to
    run, which is what keeps two independent renumbers in their original
    plan order) handles a chain of any length -- the case measured live was
    one: Current Assets wanted 1100, which Cash In Hand still held on its
    way to 1120, and plan order ran them the wrong way round. It is not
    always resolvable when a genuine cycle exists -- two or more renumbers
    each waiting on a number only another member of the same cycle is about
    to vacate. Breaking it: park the
    lowest-indexed member of the cycle onto a temporary number -- freeing
    its original number without yet reaching its own desired number -- then
    resume the topological walk; that member's own real renumber becomes
    ready, and runs, once whatever IT was waiting on has vacated in turn.

    A renumber whose `desired.number` equals its own `live.number` (a
    name-only change) never depends on itself: the dependency check compares
    against every *other* renumber in the list, never the renumber being
    checked against itself.

    Returns:
        `renumbers`, reordered, with a `ParkStep` spliced in wherever a
        cycle had to be broken. Every `RenumberStep` exposes `.live`, so a
        caller can print the sequence uniformly regardless of which kind it is.
    """
    n = len(renumbers)
    dependents, remaining = _dependency_graph(renumbers)
    # Every number this plan touches, on either side of a renumber -- a
    # park's temporary number must collide with none of them, or it would
    # just manufacture a fresh version of the same problem.
    avoid = frozenset(number for r in renumbers for number in (r.live.number, r.desired.number) if number)

    done: set[int] = set()
    vacated: set[int] = set()
    parked_at: set[str] = set()
    steps: list[RenumberStep] = []

    def _next_ready() -> int | None:
        return next((i for i in range(n) if i not in done and remaining[i] == 0), None)

    def _vacate(i: int) -> None:
        vacated.add(i)
        for dependent in dependents[i]:
            remaining[dependent] -= 1

    while len(done) < n:
        ready = _next_ready()
        if ready is not None:
            steps.append(renumbers[ready])
            done.add(ready)
            _vacate(ready)
            continue

        # Every not-yet-done account is waiting on something not yet
        # vacated: a cycle. Park the lowest-indexed member not already
        # parked -- freeing its number is what lets the rest proceed.
        to_park = next(i for i in range(n) if i not in vacated)
        candidate = _unused_number(renumbers[to_park].live.number, avoid | parked_at)
        parked_at.add(candidate)
        steps.append(ParkStep(renumbers[to_park].live, candidate))
        _vacate(to_park)

    return steps


def _dependency_graph(renumbers: list[RenumberAccount]) -> tuple[list[set[int]], list[int]]:
    """Which renumbers must run before which, and how many each is still waiting on."""
    n = len(renumbers)
    depends_on = [
        {j for j in range(n) if j != i and renumbers[i].desired.number == renumbers[j].live.number} for i in range(n)
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
