"""Order changes so each one can actually be executed when its turn comes.

Only creates need ordering, and only among themselves: ERPNext links a new
account to its parent's *document name*, so the parent must exist first.
Renumbers do not, because an account's own document name does not contain its
parent's -- renaming a parent updates its children's links without changing
their names. That is why the executor runs every renumber before any create:
by the time a child is created, its parent's document name is final.
"""

from __future__ import annotations

from stolon.apply.diff import CreateAccount
from stolon.apply.model import DesiredAccount


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
