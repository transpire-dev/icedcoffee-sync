"""The match key, which decides renumber-vs-duplicate."""

import pytest
from stolon.apply.model import DesiredAccount, LiveAccount, desired_key, live_keys


def desired(number, name, parent=None, is_group=False, root="Asset", account_type=None):
    return DesiredAccount(
        number=number,
        name=name,
        parent_number=parent,
        is_group=is_group,
        root_type=root,
        account_type=account_type,
    )


def live(document_name, name, number="", parent=None, is_group=False, root="Asset", account_type=None):
    return LiveAccount(
        document_name=document_name,
        number=number,
        name=name,
        parent_document_name=parent,
        is_group=is_group,
        root_type=root,
        account_type=account_type,
    )


def test_match_key_is_name_plus_parent_name_not_number() -> None:
    # The stock chart has no numbers, so a number-based key would match nothing
    # and every account would be created as a duplicate.
    chart = {a.number: a for a in [desired("1110", "Bank Accounts", parent="1100"), desired("1100", "Current Assets")]}
    assert desired_key(chart["1110"], chart) == ("Bank Accounts", "Current Assets")


def test_root_account_has_no_parent_in_its_key() -> None:
    chart = {"1000": desired("1000", "Assets")}
    assert desired_key(chart["1000"], chart) == ("Assets", None)


def test_same_name_under_different_parents_are_different_accounts() -> None:
    # Real case: the chart has a group "Accounts Payable" (2110) and a leaf
    # "Accounts Payable" (2111) beneath it. Keying on name alone conflates them.
    chart = {
        a.number: a
        for a in [
            desired("2100", "Current Liabilities"),
            desired("2110", "Accounts Payable", parent="2100"),
            desired("2111", "Accounts Payable", parent="2110"),
        ]
    }
    assert desired_key(chart["2110"], chart) != desired_key(chart["2111"], chart)


def test_missing_parent_is_an_error_not_a_root() -> None:
    chart = {"1110": desired("1110", "Bank Accounts", parent="9999")}
    with pytest.raises(KeyError, match="9999"):
        desired_key(chart["1110"], chart)


def test_live_keys_resolve_parents_by_document_name() -> None:
    parent = live("Current Assets - AB", "Current Assets", is_group=True)
    child = live("Bank Accounts - AB", "Bank Accounts", parent="Current Assets - AB")
    keys = live_keys([parent, child])
    assert keys[("Bank Accounts", "Current Assets")] == [child]
    assert keys[("Current Assets", None)] == [parent]


def test_live_keys_group_collisions_rather_than_dropping_them() -> None:
    # Two live accounts sharing a key is ambiguity the diff must refuse, so the
    # index keeps both instead of silently keeping the last one.
    a = live("Duplicate - AB", "Duplicate")
    b = live("1234 - Duplicate - AB", "Duplicate", number="1234")
    assert live_keys([a, b])[("Duplicate", None)] == [a, b]


def test_live_parent_outside_the_set_is_treated_as_unknown() -> None:
    orphan = live("Bank Accounts - AB", "Bank Accounts", parent="Missing - AB")
    with pytest.raises(KeyError, match="Missing - AB"):
        live_keys([orphan])
