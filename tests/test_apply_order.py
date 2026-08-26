"""Creates must be ordered parents-first; ERPNext links a child to a parent
document that has to already exist."""

import pytest
from stolon.apply.diff import CreateAccount, RenumberAccount
from stolon.apply.model import DesiredAccount, LiveAccount
from stolon.apply.order import CyclicChartError, ParkStep, depth, order_creates, order_renumbers


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


def live(document_name, number, name, parent=None):
    return LiveAccount(document_name, number, name, parent, False, "Expense", None, False)


def renumber(live_account, desired_account):
    return RenumberAccount(live_account, desired_account)


def test_the_live_current_assets_cash_in_hand_pair_orders_cash_in_hand_first() -> None:
    # Measured on the live site: apply died renumbering "Current Assets" to
    # 1100 before "Cash In Hand" -- the account already sitting at 1100 --
    # had let go of it.
    current_assets = renumber(
        live("1100-1600 - Current Assets - T", "1100-1600", "Current Assets"),
        desired("1100", "Current Assets"),
    )
    cash_in_hand = renumber(
        live("1100 - Cash In Hand - T", "1100", "Cash In Hand", parent="1100-1600 - Current Assets - T"),
        desired("1120", "Cash In Hand", parent="1100"),
    )
    ordered = order_renumbers([current_assets, cash_in_hand])
    assert [step.live.document_name for step in ordered] == [
        "1100 - Cash In Hand - T",
        "1100-1600 - Current Assets - T",
    ]


def test_a_three_link_chain_orders_correctly() -> None:
    a = renumber(live("A - T", "10", "A"), desired("20", "A"))
    b = renumber(live("B - T", "20", "B"), desired("30", "B"))
    c = renumber(live("C - T", "30", "C"), desired("40", "C"))
    ordered = order_renumbers([a, b, c])
    assert [step.live.document_name for step in ordered] == ["C - T", "B - T", "A - T"]


def test_independent_renumbers_keep_plan_order() -> None:
    x = renumber(live("X - T", "10", "X"), desired("11", "X"))
    y = renumber(live("Y - T", "20", "Y"), desired("21", "Y"))
    ordered = order_renumbers([x, y])
    assert [step.live.document_name for step in ordered] == ["X - T", "Y - T"]


def test_a_two_cycle_is_broken_with_a_park_step() -> None:
    # A wants B's number and B wants A's -- neither can go first. Parking A
    # onto a temporary number frees 10 for B, then A's real renumber (to 20)
    # runs once B has vacated it.
    a = renumber(live("A - T", "10", "A"), desired("20", "A"))
    b = renumber(live("B - T", "20", "B"), desired("10", "B"))
    ordered = order_renumbers([a, b])
    assert [type(step).__name__ for step in ordered] == ["ParkStep", "RenumberAccount", "RenumberAccount"]
    assert isinstance(ordered[0], ParkStep)
    assert ordered[0].live.document_name == "A - T"
    assert ordered[0].temporary_number == "910"
    assert ordered[1].live.document_name == "B - T"
    assert ordered[2].live.document_name == "A - T"


def test_a_name_only_change_does_not_depend_on_itself() -> None:
    # If this created a self-dependency, it would never become "ready" on
    # its own and would be mistaken for a cycle -- parked for no reason.
    rename_only = renumber(live("Old Name - T", "10", "Old Name"), desired("10", "New Name"))
    other = renumber(live("Other - T", "20", "Other"), desired("21", "Other"))
    ordered = order_renumbers([rename_only, other])
    assert [step.live.document_name for step in ordered] == ["Old Name - T", "Other - T"]
    assert isinstance(ordered[0], RenumberAccount)
