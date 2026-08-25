"""Naming and vocabulary facts, measured against ERPNext 16 on 2026-08-25."""

import pytest
from stolon.erpnext.accounts import (
    ACCOUNT_TYPES,
    ROOT_TYPES,
    UnknownAccountTypeError,
    document_name,
    validate_account_type,
)


def test_numbered_account_name_matches_the_measured_form() -> None:
    # Measured: inserting account_number="9901", account_name="ZZ Probe
    # Numbered" into company abbr "AB" produced exactly this document name.
    assert document_name("ZZ Probe Numbered", "AB", "9901") == "9901 - ZZ Probe Numbered - AB"


def test_unnumbered_account_omits_the_number_and_its_separator() -> None:
    # The stock chart on a fresh site carries no numbers at all.
    assert document_name("Bank Accounts", "AB") == "Bank Accounts - AB"


def test_empty_number_is_treated_as_no_number() -> None:
    # Frappe returns "" rather than null for an unset account_number, so the
    # empty string must not produce " - Bank Accounts - AB".
    assert document_name("Bank Accounts", "AB", "") == "Bank Accounts - AB"


def test_account_name_is_required() -> None:
    with pytest.raises(ValueError, match="account_name"):
        document_name("", "AB", "1000")


def test_abbr_is_required() -> None:
    with pytest.raises(ValueError, match="abbr"):
        document_name("Bank Accounts", "", "1000")


def test_deferred_types_are_not_account_types() -> None:
    # Measured: ERPNext rejects both with a ValidationError naming the allowed
    # set. Deferred accounting is wired through Company fields instead.
    assert "Deferred Expense" not in ACCOUNT_TYPES
    assert "Deferred Revenue" not in ACCOUNT_TYPES


def test_known_account_types_validate() -> None:
    for known in ("Bank", "Cash", "Receivable", "Payable", "Tax", "Round Off"):
        validate_account_type(known)


def test_absent_account_type_is_allowed() -> None:
    # Most accounts carry no type; ERPNext's Select permits "".
    validate_account_type(None)
    validate_account_type("")


def test_unknown_account_type_is_refused_with_the_allowed_set() -> None:
    with pytest.raises(UnknownAccountTypeError) as excinfo:
        validate_account_type("Deferred Expense")
    assert "Deferred Expense" in str(excinfo.value)
    assert "Bank" in str(excinfo.value)


def test_root_types_are_the_five_erpnext_defines() -> None:
    assert {"Asset", "Liability", "Income", "Expense", "Equity"} == ROOT_TYPES
