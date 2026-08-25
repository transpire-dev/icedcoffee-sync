"""What the diff decides, what it refuses to decide, and how it finds the pairs.

Matching is a tree walk, so every case here is stated as a tree: `chart(...)`
hangs the given accounts under one Expense root, `site(...)` hangs live ones
under the matching live root. The root pair is deliberately already in sync, so
a test that says nothing about roots produces no root changes.
"""

import pytest
from stolon.apply.diff import RefusalReason, diff
from stolon.apply.model import DesiredAccount, LiveAccount

ROOT_NUMBER = "5000"
ROOT_DOCUMENT = "5000 - Expenses - AB"


def desired(number, name, parent=ROOT_NUMBER, is_group=False, root="Expense", account_type=None):
    return DesiredAccount(number, name, parent, is_group, root, account_type)


def live(document_name, name, number="", parent=ROOT_DOCUMENT, is_group=False, root="Expense", account_type=None):
    return LiveAccount(document_name, number, name, parent, is_group, root, account_type)


def chart(*accounts):
    """The desired chart: one Expense root, plus whatever the test hangs under it."""
    return [DesiredAccount(ROOT_NUMBER, "Expenses", None, True, "Expense", None), *accounts]


def site(*accounts):
    """The live site: an Expense root already matching the chart, plus the given accounts."""
    return [LiveAccount(ROOT_DOCUMENT, ROOT_NUMBER, "Expenses", None, True, "Expense", None), *accounts]


def test_absent_account_is_created() -> None:
    plan = diff(chart(desired("5299", "UNCATEGORISED")), site(), set())
    assert [c.desired.number for c in plan.creates] == ["5299"]
    assert plan.is_applyable


def test_unnumbered_live_account_is_renumbered_not_recreated() -> None:
    # The stock chart carries no numbers at all, so matching on number would
    # match nothing and every account would be created as a duplicate.
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("Bank Charges - AB", "Bank Charges")),
        set(),
    )
    assert not plan.creates
    assert [(r.live.document_name, r.desired.number) for r in plan.renumbers] == [("Bank Charges - AB", "5202")]


def test_matching_number_and_type_produces_no_change() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges", account_type="Tax")),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202", account_type="Tax")),
        set(),
    )
    assert not plan.creates and not plan.renumbers and not plan.retypes
    assert plan.is_empty


def test_changed_account_type_is_a_retype() -> None:
    plan = diff(
        chart(desired("2142", "CA FTB LLC Tax Payable", account_type="Tax")),
        site(live("2142 - CA FTB LLC Tax Payable - AB", "CA FTB LLC Tax Payable", number="2142")),
        set(),
    )
    assert [r.desired.account_type for r in plan.retypes] == ["Tax"]


def test_live_account_not_in_the_chart_is_an_extra_not_a_deletion() -> None:
    plan = diff(chart(), site(live("Buildings - AB", "Buildings")), set())
    assert [e.live.document_name for e in plan.extras] == ["Buildings - AB"]
    assert plan.is_applyable  # extras never block; pruning is opt-in


def test_account_with_ledger_entries_is_refused_not_changed() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("Bank Charges - AB", "Bank Charges")),
        {"Bank Charges - AB"},
    )
    assert not plan.renumbers
    assert [r.reason for r in plan.refusals] == [RefusalReason.HAS_GL_ENTRIES]
    assert not plan.is_applyable


def test_untouched_account_with_ledger_entries_is_not_refused() -> None:
    # Having history is not itself a problem; changing such an account is.
    plan = diff(
        chart(desired("5202", "Bank Charges", account_type=None)),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202")),
        {"5202 - Bank Charges - AB"},
    )
    assert not plan.refusals
    assert plan.is_applyable


def test_two_live_siblings_sharing_a_name_are_refused() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(
            live("Bank Charges - AB", "Bank Charges"),
            live("1 - Bank Charges - AB", "Bank Charges", number="1"),
        ),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.AMBIGUOUS_MATCH]
    assert set(plan.refusals[0].live_document_names) == {"Bank Charges - AB", "1 - Bank Charges - AB"}


def test_same_name_under_different_parents_is_not_ambiguity() -> None:
    # Real case: the chart has a group "Accounts Payable" with a leaf of the
    # same name inside it. Ambiguity is between siblings, not chart-wide.
    plan = diff(
        chart(
            desired("2110", "Accounts Payable", is_group=True),
            desired("2111", "Accounts Payable", parent="2110"),
        ),
        site(
            live("2110 - Accounts Payable - AB", "Accounts Payable", number="2110", is_group=True),
            live(
                "2111 - Accounts Payable - AB", "Accounts Payable", number="2111", parent="2110 - Accounts Payable - AB"
            ),
        ),
        set(),
    )
    assert not plan.refusals
    assert plan.is_empty


def test_ambiguous_match_does_not_also_report_the_candidates_as_extras() -> None:
    # Both colliding live accounts are consumed by the refusal. If either one
    # slipped out of `matched`, it would reappear as an extra -- refused and
    # extra at once, which makes no sense.
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(
            live("Bank Charges - AB", "Bank Charges"),
            live("1 - Bank Charges - AB", "Bank Charges", number="1"),
        ),
        set(),
    )
    assert plan.extras == []


def test_children_of_an_ambiguous_match_are_not_extras_either() -> None:
    # The walk stops at the ambiguity, so it never looks under either
    # candidate. Reporting what it never examined as "not in the chart" would
    # be a lie.
    plan = diff(
        chart(desired("5200", "Indirect", is_group=True)),
        site(
            live("Indirect - AB", "Indirect", is_group=True),
            live("1 - Indirect - AB", "Indirect", number="1", is_group=True),
            live("Advertising - AB", "Advertising", parent="Indirect - AB"),
        ),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.AMBIGUOUS_MATCH]
    assert plan.extras == []


def test_group_flag_change_is_refused_rather_than_applied() -> None:
    # Converting a group to a leaf or back restructures the tree and can strand
    # children. It is a conversation, not a diff.
    plan = diff(
        chart(desired("5100", "Direct Costs", is_group=True)),
        site(live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=False)),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.GROUP_FLAG_CHANGED]


def test_group_flag_change_does_not_also_report_the_account_as_an_extra() -> None:
    plan = diff(
        chart(desired("5100", "Direct Costs", is_group=True)),
        site(live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=False)),
        set(),
    )
    assert plan.extras == []


def test_root_type_change_is_refused() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges", root="Expense")),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202", root="Asset")),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.ROOT_TYPE_CHANGED]


def test_root_type_change_does_not_also_report_the_account_as_an_extra() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges", root="Expense")),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202", root="Asset")),
        set(),
    )
    assert plan.extras == []


def test_renumber_and_retype_both_fire_for_the_same_account() -> None:
    # The two checks are independent `if` blocks, not an if/elif -- an
    # account that differs in both number and type must produce both a
    # RenumberAccount and a RetypeAccount, not just one.
    plan = diff(
        chart(desired("5202", "Bank Charges", account_type="Tax")),
        site(live("Bank Charges - AB", "Bank Charges", account_type="Expense")),
        set(),
    )
    assert [r.live.document_name for r in plan.renumbers] == ["Bank Charges - AB"]
    assert [r.live.document_name for r in plan.retypes] == ["Bank Charges - AB"]
    assert plan.renumbers[0].desired.number == "5202"
    assert plan.retypes[0].desired.account_type == "Tax"


def test_refusal_detail_names_the_account_and_the_conflict() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("Bank Charges - AB", "Bank Charges")),
        {"Bank Charges - AB"},
    )
    assert "Bank Charges - AB" in plan.refusals[0].detail
    assert plan.refusals[0].live_document_names == ("Bank Charges - AB",)


# --- roots ------------------------------------------------------------------
#
# ERPNext will not insert an Account without a parent_account (HTTP 417,
# MandatoryError), so a chart root can only ever be matched, never created. A
# site set up by ERPNext itself names its roots "Application of Funds
# (Assets)", "Source of Funds (Liabilities)" and "Income" -- none of which are
# our names. root_type is the only thing that identifies a root.


def test_root_matches_by_root_type_even_when_the_name_differs() -> None:
    plan = diff(
        [DesiredAccount("1000", "Assets", None, True, "Asset", None)],
        [LiveAccount("Application of Funds (Assets) - AB", "", "Application of Funds (Assets)", None, True, "Asset")],
        set(),
    )
    assert not plan.creates
    assert not plan.extras
    assert [(r.live.document_name, r.desired.number, r.desired.name) for r in plan.renumbers] == [
        ("Application of Funds (Assets) - AB", "1000", "Assets")
    ]


def test_a_child_of_a_renamed_root_matches_rather_than_duplicating() -> None:
    # The defect this walk exists to fix. Live "Current Assets - AB" hangs off
    # "Application of Funds (Assets) - AB"; the chart hangs "Current Assets"
    # off "Assets". Matched against the pre-apply live names those are two
    # different accounts, so the live one became an extra, the desired one a
    # create, and applying that duplicated the subtree. Worse, the run after
    # that matched cleanly and reported nothing to do.
    plan = diff(
        [
            DesiredAccount("1000", "Assets", None, True, "Asset", None),
            DesiredAccount("1100", "Current Assets", "1000", True, "Asset", None),
        ],
        [
            LiveAccount("Application of Funds (Assets) - AB", "", "Application of Funds (Assets)", None, True, "Asset"),
            LiveAccount(
                "Current Assets - AB", "", "Current Assets", "Application of Funds (Assets) - AB", True, "Asset"
            ),
        ],
        set(),
    )
    assert [c.desired.number for c in plan.creates] == []
    assert [e.live.document_name for e in plan.extras] == []
    assert {r.desired.number for r in plan.renumbers} == {"1000", "1100"}


def test_a_root_type_with_no_live_root_is_refused_not_created() -> None:
    plan = diff(
        [DesiredAccount("1000", "Assets", None, True, "Asset", None)],
        [LiveAccount("Income - AB", "", "Income", None, True, "Income")],
        set(),
    )
    assert not plan.creates
    assert [r.reason for r in plan.refusals] == [RefusalReason.ROOT_NOT_UNIQUE]
    assert plan.refusals[0].live_document_names == ()
    assert "parent_account" in plan.refusals[0].detail


def test_two_live_roots_of_one_root_type_are_refused() -> None:
    plan = diff(
        [DesiredAccount("1000", "Assets", None, True, "Asset", None)],
        [
            LiveAccount("Assets - AB", "", "Assets", None, True, "Asset"),
            LiveAccount("Application of Funds (Assets) - AB", "", "Application of Funds (Assets)", None, True, "Asset"),
        ],
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.ROOT_NOT_UNIQUE]
    assert set(plan.refusals[0].live_document_names) == {"Assets - AB", "Application of Funds (Assets) - AB"}
    assert plan.extras == []  # refused and unmentioned at once makes no sense
    assert not plan.creates


def test_two_chart_roots_of_one_root_type_are_a_chart_error() -> None:
    # Not a refusal: the site is fine, our own file is not.
    with pytest.raises(ValueError, match="root_type"):
        diff(
            [
                DesiredAccount("1000", "Assets", None, True, "Asset", None),
                DesiredAccount("1500", "Other Assets", None, True, "Asset", None),
            ],
            [LiveAccount("Assets - AB", "", "Assets", None, True, "Asset")],
            set(),
        )


# --- subtrees ---------------------------------------------------------------


def test_descendants_of_an_unmatched_desired_account_are_all_creates() -> None:
    plan = diff(
        chart(
            desired("5200", "Indirect", is_group=True),
            desired("5210", "Payroll", parent="5200", is_group=True),
            desired("5211", "Wages", parent="5210"),
        ),
        site(),
        set(),
    )
    assert {c.desired.number for c in plan.creates} == {"5200", "5210", "5211"}


def test_descendants_of_an_unmatched_live_account_are_all_extras() -> None:
    plan = diff(
        chart(),
        site(
            live("Buildings - AB", "Buildings", is_group=True),
            live("Roof - AB", "Roof", parent="Buildings - AB"),
        ),
        set(),
    )
    assert {e.live.document_name for e in plan.extras} == {"Buildings - AB", "Roof - AB"}
    assert plan.is_applyable


def test_a_desired_parent_outside_the_chart_is_an_error_not_a_root() -> None:
    with pytest.raises(KeyError, match="9999"):
        diff([DesiredAccount("5201", "Advertising", "9999", False, "Expense", None)], [], set())


def test_a_live_parent_outside_the_given_set_is_an_error() -> None:
    # A truncated read would otherwise look like a pile of extras.
    with pytest.raises(KeyError, match="Missing - AB"):
        diff([], [LiveAccount("Bank Charges - AB", "", "Bank Charges", "Missing - AB", False, "Expense")], set())
