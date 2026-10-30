"""Creates must be ordered parents-first; ERPNext links a child to a parent
document that has to already exist."""

import pytest

from icedcoffee_sync.apply.diff import CreateAccount, RenameAccount
from icedcoffee_sync.apply.model import DesiredAccount, LiveAccount
from icedcoffee_sync.apply.order import CyclicChartError, ParkStep, depth, order_creates, order_renames


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
    return RenameAccount(live_account, desired_account)


def _order(renames):
    """Call `order_renames` with the avoid-set built the way `apply_chart`
    builds it in production: every number these renames mention, on
    either side. Tests that need a *wider* avoid set -- to reproduce a
    collision with an account these renames never touch -- call
    `order_renames` directly instead.
    """
    chart_numbers = frozenset(r.desired.number for r in renames)
    taken = frozenset(r.live.number for r in renames if r.live.number)
    return order_renames(renames, chart_numbers, taken)


def test_the_live_current_assets_cash_in_hand_pair_orders_cash_in_hand_first() -> None:
    # Measured on the live site: apply died renumbering "Current Assets" to
    current_assets = renumber(
        live("1100-1600 - Current Assets - T", "1100-1600", "Current Assets"),
        desired("1100", "Current Assets"),
    )
    cash_in_hand = renumber(
        live("1100 - Cash In Hand - T", "1100", "Cash In Hand", parent="1100-1600 - Current Assets - T"),
        desired("1120", "Cash In Hand", parent="1100"),
    )
    ordered = _order([current_assets, cash_in_hand])
    assert [step.live.document_name for step in ordered] == [
        "1100 - Cash In Hand - T",
        "1100-1600 - Current Assets - T",
    ]


def test_a_three_link_chain_orders_correctly() -> None:
    a = renumber(live("A - T", "10", "A"), desired("20", "A"))
    b = renumber(live("B - T", "20", "B"), desired("30", "B"))
    c = renumber(live("C - T", "30", "C"), desired("40", "C"))
    ordered = _order([a, b, c])
    assert [step.live.document_name for step in ordered] == ["C - T", "B - T", "A - T"]


def test_independent_renumbers_keep_plan_order() -> None:
    x = renumber(live("X - T", "10", "X"), desired("11", "X"))
    y = renumber(live("Y - T", "20", "Y"), desired("21", "Y"))
    ordered = _order([x, y])
    assert [step.live.document_name for step in ordered] == ["X - T", "Y - T"]


def test_a_two_cycle_is_broken_with_a_park_step() -> None:
    # A wants B's number and B wants A's -- neither can go first. Parking A
    a = renumber(live("A - T", "10", "A"), desired("20", "A"))
    b = renumber(live("B - T", "20", "B"), desired("10", "B"))
    ordered = _order([a, b])
    assert [type(step).__name__ for step in ordered] == ["ParkStep", "RenameAccount", "RenameAccount"]
    assert isinstance(ordered[0], ParkStep)
    assert ordered[0].live.document_name == "A - T"
    assert ordered[0].temporary_number == "910"
    assert ordered[1].live.document_name == "B - T"
    assert ordered[2].live.document_name == "A - T"


def test_park_avoids_a_number_an_untouched_live_account_already_holds() -> None:
    a = renumber(live("A - T", "10", "A"), desired("20", "A"))
    b = renumber(live("B - T", "20", "B"), desired("10", "B"))
    ordered = order_renames([a, b], frozenset({"10", "20"}), frozenset({"10", "20", "910"}))
    park = next(step for step in ordered if isinstance(step, ParkStep))
    assert park.temporary_number == "9910"


def test_a_name_only_change_does_not_depend_on_itself() -> None:
    # If this created a self-dependency, it would never become "ready" on
    rename_only = renumber(live("Old Name - T", "10", "Old Name"), desired("10", "New Name"))
    other = renumber(live("Other - T", "20", "Other"), desired("21", "Other"))
    ordered = _order([rename_only, other])
    assert [step.live.document_name for step in ordered] == ["Old Name - T", "Other - T"]
    assert isinstance(ordered[0], RenameAccount)
