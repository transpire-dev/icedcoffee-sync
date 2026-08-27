"""Execution order, and the refusal that stops execution happening at all."""

from typing import Any

import pytest
from stolon.apply.diff import (
    ChartPlan,
    CreateAccount,
    EnableAccount,
    Refusal,
    RefusalReason,
    RenameAccount,
    RetypeAccount,
)
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
        renames=[],
        retypes=[],
        extras=[],
        refusals=[Refusal(("x", None), RefusalReason.HAS_GL_ENTRIES, "nope")],
    )
    client = FakeClient()
    with pytest.raises(PlanRefusedError, match="has_gl_entries"):
        apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    assert client.actions == []


def test_renumbers_run_before_creates() -> None:
    # A create links to its parent's document name. If the parent is being
    parent = desired("5200", "Indirect", is_group=True)
    child = desired("5201", "Advertising", parent="5200")
    by_number = {"5200": parent, "5201": child}
    plan = ChartPlan(
        creates=[CreateAccount(child)],
        renames=[RenameAccount(live("Indirect - AB", "Indirect"), parent)],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, [], by_number, "AB", "Acmebike Cycling")
    kinds = [a[0] for a in client.actions]
    assert kinds.index("call") < kinds.index("insert")


def test_create_links_to_the_parents_post_renumber_name() -> None:
    parent = desired("5200", "Indirect", is_group=True)
    child = desired("5201", "Advertising", parent="5200")
    plan = ChartPlan(
        creates=[CreateAccount(child)],
        renames=[RenameAccount(live("Indirect - AB", "Indirect"), parent)],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, [], {"5200": parent, "5201": child}, "AB", "Acmebike Cycling")
    inserted = next(doc for kind, doc in client.actions if kind == "insert")
    assert inserted["parent_account"] == "5200 - Indirect - AB"


def test_creates_are_ordered_parents_first() -> None:
    root = desired("5000", "Expenses", is_group=True)
    mid = desired("5200", "Indirect", parent="5000", is_group=True)
    leaf = desired("5201", "Advertising", parent="5200")
    plan = ChartPlan(
        creates=[CreateAccount(leaf), CreateAccount(root), CreateAccount(mid)],
        renames=[],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, [], {"5000": root, "5200": mid, "5201": leaf}, "AB", "Acmebike Cycling")
    numbers = [doc["account_number"] for kind, doc in client.actions if kind == "insert"]
    assert numbers == ["5000", "5200", "5201"]


def test_renumber_uses_the_rpc_not_a_field_write() -> None:
    # Measured: a plain update does not move the document. update_account_number
    plan = ChartPlan(
        creates=[],
        renames=[RenameAccount(live("Bank Charges - AB", "Bank Charges"), desired("5202", "Bank Charges"))],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    report = apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    method, params = next(payload for kind, payload in client.actions if kind == "call")
    assert method == "erpnext.accounts.doctype.account.account.update_account_number"
    assert params == {"name": "Bank Charges - AB", "account_name": "Bank Charges", "account_number": "5202"}
    assert report.renamed == [("Bank Charges - AB", "5202 - Bank Charges - AB")]


def test_renumbers_run_in_dependency_order() -> None:
    # Measured live: apply_chart ran plan.renames in plan order and died
    current_assets = RenameAccount(
        live("1100-1600 - Current Assets - AB", "Current Assets", "1100-1600"),
        desired("1100", "Current Assets", is_group=True),
    )
    cash_in_hand = RenameAccount(
        live("1100 - Cash In Hand - AB", "Cash In Hand", "1100"),
        desired("1120", "Cash In Hand"),
    )
    plan = ChartPlan(creates=[], renames=[current_assets, cash_in_hand], retypes=[], extras=[], refusals=[])
    client = FakeClient()
    apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    call_names = [payload[1]["name"] for kind, payload in client.actions if kind == "call"]
    assert call_names == ["1100 - Cash In Hand - AB", "1100-1600 - Current Assets - AB"]


def test_a_renumber_cycle_parks_before_either_renumber_runs() -> None:
    # A wants B's number and B wants A's -- neither can go first without
    a = RenameAccount(live("A - AB", "A", "10"), desired("20", "A"))
    b = RenameAccount(live("B - AB", "B", "20"), desired("10", "B"))
    plan = ChartPlan(creates=[], renames=[a, b], retypes=[], extras=[], refusals=[])
    client = FakeClient()
    report = apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")

    call_payloads = [payload for kind, payload in client.actions if kind == "call"]
    assert call_payloads[0][1]["account_number"] == "910"  # the park comes first
    assert call_payloads[0][1]["name"] == "A - AB"
    assert call_payloads[0][1]["account_name"] == "A"  # unchanged by the park

    # A's real renumber (last) must address the account by its post-park
    assert call_payloads[-1][1]["name"] == "910 - A - AB"
    assert call_payloads[-1][1]["account_number"] == "20"

    assert report.parked == [("A - AB", "910", "910 - A - AB")]
    assert report.renamed == [("B - AB", "10 - B - AB"), ("A - AB", "20 - A - AB")]


def test_park_step_avoids_a_number_an_untouched_live_account_already_holds() -> None:
    a = RenameAccount(live("A - AB", "A", "10"), desired("20", "A"))
    b = RenameAccount(live("B - AB", "B", "20"), desired("10", "B"))
    bystander = live("Bystander - AB", "Bystander", "910")
    plan = ChartPlan(creates=[], renames=[a, b], retypes=[], extras=[], refusals=[])
    client = FakeClient()
    report = apply_chart(client, plan, [a.live, b.live, bystander], {}, "AB", "Acmebike Cycling")

    assert report.parked == [("A - AB", "9910", "9910 - A - AB")]


def test_a_rename_carries_the_charts_name_not_just_its_number() -> None:
    # A root is matched by root_type, so the live name can be ERPNext's own
    plan = ChartPlan(
        creates=[],
        renames=[
            RenameAccount(
                live("Application of Funds (Assets) - AB", "Application of Funds (Assets)"),
                desired("1000", "Assets", is_group=True),
            )
        ],
        retypes=[],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    report = apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    _method, params = next(payload for kind, payload in client.actions if kind == "call")
    assert params == {
        "name": "Application of Funds (Assets) - AB",
        "account_name": "Assets",
        "account_number": "1000",
    }
    assert report.renamed == [("Application of Funds (Assets) - AB", "1000 - Assets - AB")]


def test_retype_writes_only_the_account_type() -> None:
    plan = ChartPlan(
        creates=[],
        renames=[],
        retypes=[RetypeAccount(live("2142 - X - AB", "X", "2142"), desired("2142", "X", account_type="Tax"))],
        extras=[],
        refusals=[],
    )
    client = FakeClient()
    apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    name, doc = next(payload for kind, payload in client.actions if kind == "update")
    assert name == "2142 - X - AB"
    assert doc == {"account_type": "Tax"}


def test_enable_writes_only_disabled_zero() -> None:
    plan = ChartPlan(
        creates=[],
        renames=[],
        retypes=[],
        extras=[],
        refusals=[],
        enables=[
            EnableAccount(live("5202 - Bank Charges - AB", "Bank Charges", "5202"), desired("5202", "Bank Charges"))
        ],
    )
    client = FakeClient()
    report = apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    name, doc = next(payload for kind, payload in client.actions if kind == "update")
    assert name == "5202 - Bank Charges - AB"
    assert doc == {"disabled": 0}
    assert report.enabled == ["5202 - Bank Charges - AB"]


def test_enable_uses_the_renamed_documents_current_name() -> None:
    # An account can be both renamed and re-enabled in the same run; the
    live_account = live("Old Name - AB", "Old Name", "10")
    desired_account = desired("20", "New Name")
    plan = ChartPlan(
        creates=[],
        renames=[RenameAccount(live_account, desired_account)],
        retypes=[],
        extras=[],
        refusals=[],
        enables=[EnableAccount(live_account, desired_account)],
    )
    client = FakeClient()
    apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    update_name, update_doc = next(payload for kind, payload in client.actions if kind == "update")
    assert update_name == "20 - New Name - AB"
    assert update_doc == {"disabled": 0}


def test_extras_are_never_touched() -> None:
    from stolon.apply.diff import ExtraAccount

    plan = ChartPlan([], [], [], [ExtraAccount(live("Buildings - AB", "Buildings"))], [])
    client = FakeClient()
    apply_chart(client, plan, [], {}, "AB", "Acmebike Cycling")
    assert client.actions == []
