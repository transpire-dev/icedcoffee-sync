"""What may be deleted, in what order, and what must not."""

from stolon.apply.model import LiveAccount
from stolon.apply.prune import PruneReason, execute_prune, plan_prune


def live(document_name, name, parent=None, is_group=False, disabled=False):
    return LiveAccount(document_name, "", name, parent, is_group, "Expense", None, disabled)


def test_an_unreferenced_leaf_is_deletable() -> None:
    leaf = live("Bank Charges - AB", "Bank Charges", parent="Expenses - AB")
    root = live("Expenses - AB", "Expenses")
    plan = plan_prune([leaf], [root, leaf], set())
    assert [d.live.document_name for d in plan.deletes] == ["Bank Charges - AB"]
    assert not plan.refusals


def test_an_account_with_ledger_entries_is_refused() -> None:
    leaf = live("Bank Charges - AB", "Bank Charges", parent="Expenses - AB")
    plan = plan_prune([leaf], [leaf], {"Bank Charges - AB"})
    assert not plan.deletes
    assert [r.reason for r in plan.refusals] == [PruneReason.HAS_GL_ENTRIES]


def test_a_root_is_never_deleted() -> None:
    # ERPNext requires its roots, and the engine matches rather than creates
    # them -- a root appearing as an extra means something is wrong upstream.
    root = live("Orphan Root - AB", "Orphan Root")
    plan = plan_prune([root], [root], set())
    assert [r.reason for r in plan.refusals] == [PruneReason.IS_ROOT]


def test_a_whole_extra_subtree_is_deletable_deepest_first() -> None:
    root = live("Expenses - AB", "Expenses")
    group = live("Old Group - AB", "Old Group", parent="Expenses - AB", is_group=True)
    child = live("Old Leaf - AB", "Old Leaf", parent="Old Group - AB")
    plan = plan_prune([group, child], [root, group, child], set())
    assert [d.live.document_name for d in plan.deletes] == ["Old Leaf - AB", "Old Group - AB"]


def test_a_parent_whose_child_survives_is_refused() -> None:
    # Defensive: tree matching should make this unreachable, so it fires only
    # when that reasoning is wrong.
    root = live("Expenses - AB", "Expenses")
    group = live("Old Group - AB", "Old Group", parent="Expenses - AB", is_group=True)
    keeper = live("Kept - AB", "Kept", parent="Old Group - AB")
    plan = plan_prune([group], [root, group, keeper], set())
    assert [r.reason for r in plan.refusals] == [PruneReason.HAS_LIVE_CHILD]


def test_a_refused_account_does_not_block_the_others() -> None:
    # The opposite of ChartPlan on purpose: deletions are independent.
    root = live("Expenses - AB", "Expenses")
    busy = live("Busy - AB", "Busy", parent="Expenses - AB")
    idle = live("Idle - AB", "Idle", parent="Expenses - AB")
    plan = plan_prune([busy, idle], [root, busy, idle], {"Busy - AB"})
    assert [d.live.document_name for d in plan.deletes] == ["Idle - AB"]
    assert len(plan.refusals) == 1


def test_an_already_disabled_extra_is_skipped_entirely() -> None:
    # Neither a delete nor a refusal: a prior run (or execute_prune's own
    # fallback) already retired it, and there is nothing left to decide.
    leaf = live("Cash - AB", "Cash", parent="Expenses - AB", disabled=True)
    plan = plan_prune([leaf], [leaf], set())
    assert not plan.deletes
    assert not plan.refusals


class FakeClient:
    """A client whose delete and/or update can be told to fail by name."""

    def __init__(self, failing_delete=(), failing_update=()):
        self.failing_delete = set(failing_delete)
        self.failing_update = set(failing_update)
        self.deleted: list[str] = []
        self.updated: list[str] = []

    def delete(self, doctype: str, name: str):
        from stolon.frappe.errors import LinkExistsError

        if name in self.failing_delete:
            raise LinkExistsError("still linked")
        self.deleted.append(name)
        return {}

    def update(self, doctype: str, name: str, doc: dict):
        from stolon.frappe.errors import ValidationError

        if name in self.failing_update:
            raise ValidationError("cannot disable either")
        self.updated.append(name)
        return {}


def test_execute_deletes_in_plan_order() -> None:
    root = live("Expenses - AB", "Expenses")
    group = live("Old Group - AB", "Old Group", parent="Expenses - AB", is_group=True)
    child = live("Old Leaf - AB", "Old Leaf", parent="Old Group - AB")
    plan = plan_prune([group, child], [root, group, child], set())
    client = FakeClient()
    report = execute_prune(client, plan)
    assert client.deleted == ["Old Leaf - AB", "Old Group - AB"]
    assert report.deleted == ["Old Leaf - AB", "Old Group - AB"]
    assert report.disabled == []
    assert report.failed == []


def test_a_failed_delete_falls_back_to_disable_and_the_run_continues() -> None:
    # ERPNext refuses to delete some accounts outright ("You can disable
    # this Account instead of deleting it"). That is a disable, not a
    # failure, and the run still reaches the next account.
    root = live("Expenses - AB", "Expenses")
    a = live("A - AB", "A", parent="Expenses - AB")
    b = live("B - AB", "B", parent="Expenses - AB")
    plan = plan_prune([a, b], [root, a, b], set())
    client = FakeClient(failing_delete={"A - AB"})
    report = execute_prune(client, plan)
    assert "B - AB" in report.deleted
    assert report.disabled == ["A - AB"]
    assert report.failed == []
    assert client.updated == ["A - AB"]


def test_a_delete_that_fails_and_a_disable_that_also_fails_is_recorded_as_failed() -> None:
    root = live("Expenses - AB", "Expenses")
    a = live("A - AB", "A", parent="Expenses - AB")
    plan = plan_prune([a], [root, a], set())
    client = FakeClient(failing_delete={"A - AB"}, failing_update={"A - AB"})
    report = execute_prune(client, plan)
    assert report.deleted == []
    assert report.disabled == []
    assert [name for name, _ in report.failed] == ["A - AB"]
    assert report.failed[0][1] == "cannot disable either"
