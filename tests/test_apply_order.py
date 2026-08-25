"""Creates must be ordered parents-first; ERPNext links a child to a parent
document that has to already exist."""

import pytest
from stolon.apply.diff import CreateAccount
from stolon.apply.model import DesiredAccount
from stolon.apply.order import CyclicChartError, depth, order_creates


def desired(number, name, parent=None):
    return DesiredAccount(number, name, parent, False, "Expense", None)


CHART = {
    "5000": desired("5000", "Expenses"),
    "5200": desired("5200", "Indirect", parent="5000"),
    "5201": desired("5201", "Advertising", parent="5200"),
}


def test_root_is_depth_zero() -> None:
    assert depth(CHART["5000"], CHART) == 0


def test_depth_counts_ancestors() -> None:
    assert depth(CHART["5201"], CHART) == 2


def test_creates_are_ordered_parents_first() -> None:
    shuffled = [CreateAccount(CHART[n]) for n in ("5201", "5000", "5200")]
    assert [c.desired.number for c in order_creates(shuffled, CHART)] == ["5000", "5200", "5201"]


def test_ordering_is_stable_within_a_depth() -> None:
    chart = {
        "5000": desired("5000", "Expenses"),
        "5210": desired("5210", "Payroll", parent="5000"),
        "5201": desired("5201", "Advertising", parent="5000"),
    }
    creates = [CreateAccount(chart["5210"]), CreateAccount(chart["5201"])]
    assert [c.desired.number for c in order_creates(creates, chart)] == ["5210", "5201"]


def test_a_cycle_raises_rather_than_looping() -> None:
    cyclic = {
        "A": DesiredAccount("A", "A", "B", True, "Expense", None),
        "B": DesiredAccount("B", "B", "A", True, "Expense", None),
    }
    with pytest.raises(CyclicChartError, match="A"):
        depth(cyclic["A"], cyclic)
