"""What the diff decides, what it refuses to decide, and how it finds the pairs.

Matching is a tree walk, so every case here is stated as a tree: `chart(...)`
hangs the given accounts under one Expense root, `site(...)` hangs live ones
under the matching live root. The root pair is deliberately already in sync, so
a test that says nothing about roots produces no root changes.
"""

import pytest

from icedcoffee_sync.apply.diff import RefusalReason, diff
from icedcoffee_sync.apply.model import DesiredAccount, LiveAccount

ROOT_NUMBER = "5000"
ROOT_DOCUMENT = "5000 - Expenses - AB"


def desired(number, name, parent=ROOT_NUMBER, is_group=False, root="Expense", account_type=None):
    return DesiredAccount(number, name, parent, is_group, root, account_type)


def live(
    document_name,
    name,
    number="",
    parent=ROOT_DOCUMENT,
    is_group=False,
    root="Expense",
    account_type=None,
    disabled=False,
):
    return LiveAccount(document_name, number, name, parent, is_group, root, account_type, disabled)


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
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("Bank Charges - AB", "Bank Charges")),
        set(),
    )
    assert not plan.creates
    assert [(r.live.document_name, r.desired.number) for r in plan.renames] == [("Bank Charges - AB", "5202")]


def test_matching_number_and_type_produces_no_change() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges", account_type="Tax")),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202", account_type="Tax")),
        set(),
    )
    assert not plan.creates and not plan.renames and not plan.retypes
    assert plan.is_empty


def test_a_disabled_match_is_reported_for_enabling() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202", disabled=True)),
        set(),
    )
    assert [e.live.document_name for e in plan.enables] == ["5202 - Bank Charges - AB"]
    assert not plan.is_empty
    assert plan.is_applyable


def test_an_enabled_match_needing_no_other_change_reports_no_enable() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202", disabled=False)),
        set(),
    )
    assert plan.enables == []
    assert plan.is_empty


def test_a_disabled_match_that_also_needs_a_retype_reports_both() -> None:
    plan = diff(
        chart(desired("2142", "CA FTB LLC Tax Payable", account_type="Tax")),
        site(live("2142 - CA FTB LLC Tax Payable - AB", "CA FTB LLC Tax Payable", number="2142", disabled=True)),
        set(),
    )
    assert [r.desired.account_type for r in plan.retypes] == ["Tax"]
    assert [e.live.document_name for e in plan.enables] == ["2142 - CA FTB LLC Tax Payable - AB"]


def test_a_disabled_account_carrying_ledger_entries_is_still_enabled() -> None:
    # Enabling never touches GL entries or the document's identity, unlike
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("5202 - Bank Charges - AB", "Bank Charges", number="5202", disabled=True)),
        {"5202 - Bank Charges - AB"},
    )
    assert [e.live.document_name for e in plan.enables] == ["5202 - Bank Charges - AB"]
    assert not plan.refusals


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
    assert not plan.renames
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


def test_renamed_account_keeping_its_number_is_renamed_not_recreated() -> None:
    plan = diff(
        chart(desired("5219", "Research & Development Tools")),
        site(live("5219 - R&D Software - AB", "R&D Software", number="5219")),
        set(),
    )
    assert not plan.creates
    assert not plan.extras
    assert [(r.live.document_name, r.desired.name) for r in plan.renames] == [
        ("5219 - R&D Software - AB", "Research & Development Tools")
    ]
    assert plan.is_applyable


def test_a_rename_that_keeps_its_number_is_not_a_renumber() -> None:
    plan = diff(
        chart(desired("5219", "Research & Development Tools")),
        site(live("5219 - R&D Software - AB", "R&D Software", number="5219")),
        set(),
    )
    (change,) = plan.renames
    assert change.changes_name
    assert not change.changes_number


def test_a_renumber_that_keeps_its_name_is_not_a_rename() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("5150 - Bank Charges - AB", "Bank Charges", number="5150")),
        set(),
    )
    (change,) = plan.renames
    assert change.changes_number
    assert not change.changes_name


def test_a_root_can_change_both_its_number_and_its_name() -> None:
    # Roots are the only accounts where both move at once. Everything else is
    # paired on one of the two, so whichever did the pairing still matches --
    # a child changing both is not a rename at all, it is a create and an
    # extra (test_changing_both_number_and_name_is_a_create_not_a_rename).
    # A root is matched on root_type instead, which leaves both free.
    #
    # In practice this is the stock-site adoption path and nothing else. The
    # five roots are what the accounting standard says they are, so moving one
    # would mean the company had changed country and standard both -- IFRS
    # instead of GAAP. That is a new chart, not an edit to this one.
    plan = diff(
        [DesiredAccount("1000", "Assets", None, True, "Asset", None)],
        [LiveAccount("Application of Funds (Assets) - AB", "", "Application of Funds (Assets)", None, True, "Asset")],
        set(),
    )
    (change,) = plan.renames
    assert change.changes_number
    assert change.changes_name


def test_a_rename_does_not_collide_with_the_account_being_renamed() -> None:
    # The failure this whole fallback exists to stop: an unmatched rename became
    # a create, the old account stayed behind holding the number, and the create
    # refused against it. The plan then said "impossible" about its own subject.
    plan = diff(
        chart(desired("5216", "Travel, Conferences & Professional Development")),
        site(live("5216 - Travel & Conference Expenses - AB", "Travel & Conference Expenses", number="5216")),
        set(),
    )
    assert not plan.refusals
    assert plan.is_applyable


def test_two_chart_entries_contesting_one_live_account_are_refused() -> None:
    # 5203 claims the live 5203 by number while 5202 claims the same account by
    # name, and there is no way to tell whether the author meant to rename 5203
    # or to renumber "Bank Charges". Guessing the number would repurpose the
    # document, carrying every non-GL link on it -- a Bank Account record, a tax
    # template, a Company default -- to the wrong account.
    plan = diff(
        chart(
            desired("5203", "Depreciation"),
            desired("5202", "Bank Charges"),
        ),
        site(live("5203 - Bank Charges - AB", "Bank Charges", number="5203")),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.AMBIGUOUS_MATCH]
    assert "5203 - Bank Charges - AB" in plan.refusals[0].detail
    assert not plan.is_applyable


def test_a_name_match_on_an_uncontested_sibling_still_pairs() -> None:
    # The other half of the rule above: a name match is only contested when
    # some other chart entry owns that same account by number. Here nothing
    # does, so the unnumbered stock account is adopted as usual.
    plan = diff(
        chart(
            desired("5203", "Depreciation"),
            desired("5202", "Bank Charges"),
        ),
        site(
            live("5203 - Depreciation - AB", "Depreciation", number="5203"),
            live("Bank Charges - AB", "Bank Charges"),
        ),
        set(),
    )
    assert not plan.refusals
    assert [(r.live.document_name, r.desired.number) for r in plan.renames] == [("Bank Charges - AB", "5202")]


def test_a_number_match_settles_two_siblings_sharing_a_name() -> None:
    # Same name twice is ambiguous only when nothing better is available. The
    # number says which one is meant, so this is a match rather than a refusal.
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(
            live("5202 - Bank Charges - AB", "Bank Charges", number="5202"),
            live("Bank Charges - AB", "Bank Charges"),
        ),
        set(),
    )
    assert not plan.refusals
    assert [e.live.document_name for e in plan.extras] == ["Bank Charges - AB"]


def test_two_accounts_swapping_names_are_two_renames() -> None:
    # Number-first makes this the obvious thing: each account keeps its number
    # and takes its new name. Matching on name would swap the numbers instead,
    # renumbering two accounts to say what renaming them says.
    plan = diff(
        chart(
            desired("5110", "Production Hosting"),
            desired("5150", "Development Hosting"),
        ),
        site(
            live("5110 - Development Hosting - AB", "Development Hosting", number="5110"),
            live("5150 - Production Hosting - AB", "Production Hosting", number="5150"),
        ),
        set(),
    )
    assert not plan.creates and not plan.extras and not plan.refusals
    assert {(r.live.document_name, r.desired.name) for r in plan.renames} == {
        ("5110 - Development Hosting - AB", "Production Hosting"),
        ("5150 - Production Hosting - AB", "Development Hosting"),
    }


def test_renaming_an_account_with_ledger_entries_is_refused_not_duplicated() -> None:
    # Before the number fallback this was a silent create beside the account
    # with the history, which is the worst available outcome.
    plan = diff(
        chart(desired("5219", "Research & Development Tools")),
        site(live("5219 - R&D Software - AB", "R&D Software", number="5219")),
        {"5219 - R&D Software - AB"},
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.HAS_GL_ENTRIES]
    assert not plan.creates


def test_changing_both_number_and_name_is_a_create_not_a_rename() -> None:
    # Neither key survives, so there is nothing tying the two together and
    # guessing would be worse than creating. The old account is an extra.
    plan = diff(
        chart(desired("5230", "Something Else")),
        site(live("5219 - R&D Software - AB", "R&D Software", number="5219")),
        set(),
    )
    assert [c.desired.number for c in plan.creates] == ["5230"]
    assert [e.live.document_name for e in plan.extras] == ["5219 - R&D Software - AB"]


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
    plan = diff(
        chart(desired("5202", "Bank Charges", account_type="Tax")),
        site(live("Bank Charges - AB", "Bank Charges", account_type="Expense")),
        set(),
    )
    assert [r.live.document_name for r in plan.renames] == ["Bank Charges - AB"]
    assert [r.live.document_name for r in plan.retypes] == ["Bank Charges - AB"]
    assert plan.renames[0].desired.number == "5202"
    assert plan.retypes[0].desired.account_type == "Tax"


def test_refusal_detail_names_the_account_and_the_conflict() -> None:
    plan = diff(
        chart(desired("5202", "Bank Charges")),
        site(live("Bank Charges - AB", "Bank Charges")),
        {"Bank Charges - AB"},
    )
    assert "Bank Charges - AB" in plan.refusals[0].detail
    assert plan.refusals[0].live_document_names == ("Bank Charges - AB",)


# --- number collisions -------------------------------------------------------


def test_create_whose_number_is_held_under_another_parent_is_refused() -> None:
    # Matching is sibling-local, so a number held somewhere else in the tree is
    # out of its reach: the chart's 5299 is a create, and only this check sees
    # the account already sitting on that number under a different parent.
    plan = diff(
        chart(
            desired("5100", "Direct Costs", is_group=True),
            desired("5299", "UNCATEGORISED", parent="5100"),
            desired("5200", "Indirect Expenses", is_group=True),
        ),
        site(
            live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=True),
            live("5200 - Indirect Expenses - AB", "Indirect Expenses", number="5200", is_group=True),
            live(
                "5299 - Something Else - AB",
                "Something Else",
                number="5299",
                parent="5200 - Indirect Expenses - AB",
            ),
        ),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.NUMBER_COLLISION]
    assert "UNCATEGORISED" in plan.refusals[0].detail
    assert "5299 - Something Else - AB" in plan.refusals[0].detail
    assert plan.refusals[0].live_document_names == ("5299 - Something Else - AB",)
    assert not plan.is_applyable


def test_a_sibling_holding_the_number_is_renamed_into_it_rather_than_refused() -> None:
    # The same shape as the test above, one parent instead of two -- and the
    # opposite outcome, because within a parent the number reaches the account
    # and says it is the same one. This is what makes a chart rename appliable.
    plan = diff(
        chart(desired("5299", "UNCATEGORISED")),
        site(live("5299 - Something Else - AB", "Something Else", number="5299")),
        set(),
    )
    assert not plan.refusals
    assert not plan.creates
    assert [(r.live.document_name, r.desired.name) for r in plan.renames] == [
        ("5299 - Something Else - AB", "UNCATEGORISED")
    ]


def test_create_whose_number_is_vacated_by_a_renumber_in_the_same_plan_is_not_refused() -> None:
    # "Old Category" moves to another group and takes 5301 with it, freeing
    # 5299 for the new account in the group it left. Without the vacated-number
    # exemption a valid plan like this would refuse itself forever.
    plan = diff(
        chart(
            desired("5100", "Direct Costs", is_group=True),
            desired("5301", "Old Category", parent="5100"),
            desired("5200", "Indirect Expenses", is_group=True),
            desired("5299", "UNCATEGORISED", parent="5200"),
        ),
        site(
            live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=True),
            live(
                "5299 - Old Category - AB",
                "Old Category",
                number="5299",
                parent="5100 - Direct Costs - AB",
            ),
            live("5200 - Indirect Expenses - AB", "Indirect Expenses", number="5200", is_group=True),
        ),
        set(),
    )
    assert [c.desired.number for c in plan.creates] == ["5299"]
    assert [(r.live.document_name, r.desired.number) for r in plan.renames] == [("5299 - Old Category - AB", "5301")]
    assert not plan.refusals
    assert plan.is_applyable


def test_renumbering_an_account_while_reusing_its_old_number_is_refused() -> None:
    # Asked to move "Old Category" to 5301 and put a new account on the 5299
    # it leaves behind, within one parent, both chart entries point at the one
    # live document: 5299 by number, 5301 by name. Preferring the number would
    # keep that document and relabel it UNCATEGORISED, dragging whatever links
    # to it along. Split into two applies -- renumber first, then add -- and
    # each step is unambiguous.
    plan = diff(
        chart(
            desired("5301", "Old Category"),
            desired("5299", "UNCATEGORISED"),
        ),
        site(live("5299 - Old Category - AB", "Old Category", number="5299")),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.AMBIGUOUS_MATCH]
    assert "two separate changes" in plan.refusals[0].detail
    assert not plan.is_applyable


def test_matched_account_keeping_its_own_number_is_not_reported_as_colliding_with_itself() -> None:
    # A root matched by root_type but renamed still produces a RenameAccount
    plan = diff(
        [DesiredAccount("1000", "Assets", None, True, "Asset", None)],
        [
            LiveAccount(
                "1000 - Application of Funds (Assets) - AB",
                "1000",
                "Application of Funds (Assets)",
                None,
                True,
                "Asset",
            )
        ],
        set(),
    )
    assert [(r.live.document_name, r.desired.number) for r in plan.renames] == [
        ("1000 - Application of Funds (Assets) - AB", "1000")
    ]
    assert not plan.refusals
    assert plan.is_applyable


def test_renumber_whose_target_number_is_held_under_another_parent_is_refused() -> None:
    # The unnumbered stock "Payroll" is matched by name and numbered 5210 --
    # but 5210 is already taken, in a part of the tree the sibling-local match
    # never looked at.
    plan = diff(
        chart(
            desired("5100", "Direct Costs", is_group=True),
            desired("5210", "Payroll", parent="5100"),
            desired("5200", "Indirect Expenses", is_group=True),
        ),
        site(
            live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=True),
            live("Payroll - AB", "Payroll", parent="5100 - Direct Costs - AB"),
            live("5200 - Indirect Expenses - AB", "Indirect Expenses", number="5200", is_group=True),
            live(
                "5210 - Something Else - AB",
                "Something Else",
                number="5210",
                parent="5200 - Indirect Expenses - AB",
            ),
        ),
        set(),
    )
    assert [r.reason for r in plan.refusals] == [RefusalReason.NUMBER_COLLISION]
    assert "Payroll" in plan.refusals[0].detail
    assert "5210 - Something Else - AB" in plan.refusals[0].detail
    assert not plan.is_applyable


def test_number_collision_refusal_makes_the_whole_plan_unapplyable() -> None:
    # A single collision blocks the plan even when everything else in it is
    plan = diff(
        chart(
            desired("5100", "Direct Costs", is_group=True),
            desired("5299", "UNCATEGORISED", parent="5100"),
            desired("5301", "Legit New Account", parent="5100"),
            desired("5200", "Indirect Expenses", is_group=True),
        ),
        site(
            live("5100 - Direct Costs - AB", "Direct Costs", number="5100", is_group=True),
            live("5200 - Indirect Expenses - AB", "Indirect Expenses", number="5200", is_group=True),
            live(
                "5299 - Something Else - AB",
                "Something Else",
                number="5299",
                parent="5200 - Indirect Expenses - AB",
            ),
        ),
        set(),
    )
    assert {c.desired.number for c in plan.creates} == {"5299", "5301"}
    assert [r.reason for r in plan.refusals] == [RefusalReason.NUMBER_COLLISION]
    assert not plan.is_applyable


# --- roots ------------------------------------------------------------------


def test_root_matches_by_root_type_even_when_the_name_differs() -> None:
    plan = diff(
        [DesiredAccount("1000", "Assets", None, True, "Asset", None)],
        [LiveAccount("Application of Funds (Assets) - AB", "", "Application of Funds (Assets)", None, True, "Asset")],
        set(),
    )
    assert not plan.creates
    assert not plan.extras
    assert [(r.live.document_name, r.desired.number, r.desired.name) for r in plan.renames] == [
        ("Application of Funds (Assets) - AB", "1000", "Assets")
    ]


def test_a_child_of_a_renamed_root_matches_rather_than_duplicating() -> None:
    # The defect this walk exists to fix. Live "Current Assets - AB" hangs off
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
    assert {r.desired.number for r in plan.renames} == {"1000", "1100"}


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
