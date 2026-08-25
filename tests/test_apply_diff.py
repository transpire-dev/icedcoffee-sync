"""What the diff decides, and what it refuses to decide."""

from stolon.apply.diff import RefusalReason, diff
from stolon.apply.model import DesiredAccount, LiveAccount


def desired(number, name, parent=None, is_group=False, root="Expense", account_type=None):
    return DesiredAccount(number, name, parent, is_group, root, account_type)


def live(document_name, name, number="", parent=None, is_group=False, root="Expense", account_type=None):
    return LiveAccount(document_name, number, name, parent, is_group, root, account_type)


def test_absent_account_is_created() -> None:
    plan = diff([desired("5299", "UNCATEGORISED")], [], set())
    assert [c.desired.number for c in plan.creates] == ["5299"]
    assert plan.is_applyable


def test_unnumbered_live_account_is_renumbered_not_recreated() -> None:
    # The whole point of the match key: the stock chart has no numbers.
    plan = diff(
        [desired("5202", "Bank Charges")],
        [live("Bank Charges - AB", "Bank Charges")],
        set(),
    )
    assert not plan.creates
    assert [(r.live.document_name, r.desired.number) for r in plan.renumbers] == [("Bank Charges - AB", "5202")]


def test_matching_number_and_type_produces_no_change() -> None:
    plan = diff(
        [desired("5202", "Bank Charges", account_type="Tax")],
        [live("5202 - Bank Charges - AB", "Bank Charges", number="5202", account_type="Tax")],
        set(),
    )
    assert not plan.creates and not plan.renumbers and not plan.retypes


def test_changed_account_type_is_a_retype() -> None:
    plan = diff(
        [desired("2142", "CA FTB LLC Tax Payable", account_type="Tax")],
        [live("2142 - CA FTB LLC Tax Payable - AB", "CA FTB LLC Tax Payable", number="2142")],
        set(),
    )
    assert [r.desired.account_type for r in plan.retypes] == ["Tax"]


def test_live_account_not_in_the_chart_is_an_extra_not_a_deletion() -> None:
    plan = diff([], [live("Buildings - AB", "Buildings")], set())
    assert [e.live.document_name for e in plan.extras] == ["Buildings - AB"]
    assert plan.is_applyable  # extras never block; pruning is opt-in


def test_account_with_ledger_entries_is_refused_not_changed() -> None:
    plan = diff(
        [desired("5202", "Bank Charges")],
        [live("Bank Charges - AB", "Bank Charges")],
        {"Bank Charges - AB"},
    )
    assert not plan.renumbers
    assert [r.reason for r in plan.refusals] == [RefusalReason.HAS_GL_ENTRIES]
    assert not plan.is_applyable


def test_untouched_account_with_ledger_entries_is_not_refused() -> None:
    # Having history is not itself a problem; changing such an account is.
    plan = diff(
        [desired("5202", "Bank Charges", account_type=None)],
        [live("5202 - Bank Charges - AB", "Bank Charges", number="5202")],
        {"5202 - Bank Charges - AB"},
    )
    assert not plan.refusals
    assert plan.is_applyable


def test_two_live_accounts_sharing_a_key_are_refused() -> None:
    plan = diff(
        [desired("5202", "Bank Charges")],
        [live("Bank Charges - AB", "Bank Charges"), live("1 - Bank Charges - AB", "Bank Charges", number="1")],
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.AMBIGUOUS_MATCH]


def test_ambiguous_match_does_not_also_report_the_candidates_as_extras() -> None:
    # Both colliding live accounts are consumed by the refusal. If either one
    # slipped out of `matched`, it would reappear as an extra -- refused and
    # extra at once, which makes no sense.
    plan = diff(
        [desired("5202", "Bank Charges")],
        [live("Bank Charges - AB", "Bank Charges"), live("1 - Bank Charges - AB", "Bank Charges", number="1")],
        set(),
    )
    assert plan.extras == []


def test_group_flag_change_is_refused_rather_than_applied() -> None:
    # Converting a group to a leaf or back restructures the tree and can strand
    # children. It is a conversation, not a diff.
    plan = diff(
        [desired("5100", "Direct Costs", is_group=True)],
        [live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=False)],
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.GROUP_FLAG_CHANGED]


def test_group_flag_change_does_not_also_report_the_account_as_an_extra() -> None:
    plan = diff(
        [desired("5100", "Direct Costs", is_group=True)],
        [live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=False)],
        set(),
    )
    assert plan.extras == []


def test_root_type_change_is_refused() -> None:
    plan = diff(
        [desired("5202", "Bank Charges", root="Expense")],
        [live("5202 - Bank Charges - AB", "Bank Charges", number="5202", root="Asset")],
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.ROOT_TYPE_CHANGED]


def test_root_type_change_does_not_also_report_the_account_as_an_extra() -> None:
    plan = diff(
        [desired("5202", "Bank Charges", root="Expense")],
        [live("5202 - Bank Charges - AB", "Bank Charges", number="5202", root="Asset")],
        set(),
    )
    assert plan.extras == []


def test_renumber_and_retype_both_fire_for_the_same_account() -> None:
    # The two checks are independent `if` blocks, not an if/elif -- an
    # account that differs in both number and type must produce both a
    # RenumberAccount and a RetypeAccount, not just one.
    plan = diff(
        [desired("5202", "Bank Charges", account_type="Tax")],
        [live("Bank Charges - AB", "Bank Charges", account_type="Expense")],
        set(),
    )
    assert [r.live.document_name for r in plan.renumbers] == ["Bank Charges - AB"]
    assert [r.live.document_name for r in plan.retypes] == ["Bank Charges - AB"]
    assert plan.renumbers[0].desired.number == "5202"
    assert plan.retypes[0].desired.account_type == "Tax"


def test_refusal_detail_names_the_account_and_the_conflict() -> None:
    plan = diff(
        [desired("5202", "Bank Charges")],
        [live("Bank Charges - AB", "Bank Charges")],
        {"Bank Charges - AB"},
    )
    assert "Bank Charges - AB" in plan.refusals[0].detail
