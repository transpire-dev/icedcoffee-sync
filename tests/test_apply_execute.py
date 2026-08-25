"""Execution order, and the refusal that stops execution happening at all."""

from typing import Any

import pytest
from stolon.apply.diff import ChartPlan, CreateAccount, Refusal, RefusalReason, RenumberAccount, RetypeAccount
from stolon.apply.execute import PlanRefusedError, apply_chart
from stolon.apply.model import DesiredAccount, LiveAccount


def desired(number, name, parent=None, is_group=False, account_type=None):
    return DesiredAccount(number, name, parent, is_group, "Expense", account_type)


def live(document_name, name, number=""):
    return LiveAccount(document_name, number, name, None, False, "Expense", None)


class FakeClient:
    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []

    def insert(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]:
        self.actions.append(("insert", doc))
        return {"name": f"{doc['account_number']} - {doc['account_name']} - AB"}

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> dict[str, Any]:
        self.actions.append(("update", (name, doc)))
        return {"name": name}

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.actions.append(("call", (method, params)))
        params = params or {}
        return f"{params['account_number']} - {params['account_name']} - AB"


def test_a_refused_plan_is_not_executed_at_all() -> None:
    plan = ChartPlan(
        creates=[CreateAccount(desired("5299", "UNCATEGORISED"))],
        renumbers=[],
        retypes=[],
        extras=[],
        refusals=[Refusal(("x", None), RefusalReason.HAS_GL_ENTRIES, "nope")],
    )
    client = FakeClient()
    with pytest.raises(PlanRefusedError, match="has_gl_entries"):
        apply_chart(client, plan, {}, "AB", "Acmebike Cycling")
    assert client.actions == []


def test_renumbers_run_before_creates() -> None:
    # A create links to its parent's document name. If the parent is being
    # renumbered, doing the create first links to a name about to disappear.
    parent = desired("5200", "Indirect", is_group=True)
    child = desired("5201", "Advertising", parent="5200")
    by_number = {"5200": parent, "5201": child}
    plan = ChartPlan(
        creates=[CreateAccount(child)],
        renumbers=[RenumberAccount(live("Indirect - AB", "Indirect"), parent)],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, by_number, "AB", "Acmebike Cycling")
    kinds = [a[0] for a in client.actions]
    assert kinds.index("call") < kinds.index("insert")


def test_create_links_to_the_parents_post_renumber_name() -> None:
    parent = desired("5200", "Indirect", is_group=True)
    child = desired("5201", "Advertising", parent="5200")
    plan = ChartPlan(
        creates=[CreateAccount(child)],
        renumbers=[RenumberAccount(live("Indirect - AB", "Indirect"), parent)],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, {"5200": parent, "5201": child}, "AB", "Acmebike Cycling")
    inserted = next(doc for kind, doc in client.actions if kind == "insert")
    assert inserted["parent_account"] == "5200 - Indirect - AB"


def test_creates_are_ordered_parents_first() -> None:
    root = desired("5000", "Expenses", is_group=True)
    mid = desired("5200", "Indirect", parent="5000", is_group=True)
    leaf = desired("5201", "Advertising", parent="5200")
    plan = ChartPlan(
        creates=[CreateAccount(leaf), CreateAccount(root), CreateAccount(mid)],
        renumbers=[],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, {"5000": root, "5200": mid, "5201": leaf}, "AB", "Acmebike Cycling")
    numbers = [doc["account_number"] for kind, doc in client.actions if kind == "insert"]
    assert numbers == ["5000", "5200", "5201"]


def test_renumber_uses_the_rpc_not_a_field_write() -> None:
    # Measured: a plain update does not move the document. update_account_number
    # renames it and returns the new name.
    plan = ChartPlan(
        creates=[],
        renumbers=[RenumberAccount(live("Bank Charges - AB", "Bank Charges"), desired("5202", "Bank Charges"))],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    report = apply_chart(client, plan, {}, "AB", "Acmebike Cycling")
    method, params = next(payload for kind, payload in client.actions if kind == "call")
    assert method == "erpnext.accounts.doctype.account.account.update_account_number"
    assert params == {"name": "Bank Charges - AB", "account_name": "Bank Charges", "account_number": "5202"}
    assert report.renumbered == [("Bank Charges - AB", "5202 - Bank Charges - AB")]


def test_retype_writes_only_the_account_type() -> None:
    plan = ChartPlan(
        creates=[],
        renumbers=[],
        retypes=[RetypeAccount(live("2142 - X - AB", "X", "2142"), desired("2142", "X", account_type="Tax"))],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, {}, "AB", "Acmebike Cycling")
    name, doc = next(payload for kind, payload in client.actions if kind == "update")
    assert name == "2142 - X - AB"
    assert doc == {"account_type": "Tax"}


def test_extras_are_never_touched() -> None:
    from stolon.apply.diff import ExtraAccount

    plan = ChartPlan([], [], [], [ExtraAccount(live("Buildings - AB", "Buildings"))], [])
    client = FakeClient()
    apply_chart(client, plan, {}, "AB", "Acmebike Cycling")
    assert client.actions == []
