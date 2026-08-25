"""What may be deleted, in what order, and what must not."""

import pytest
from stolon.apply.model import LiveAccount
from stolon.apply.prune import DeleteAccount, PrunePlan, PruneReason, execute_prune, plan_prune


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
    """A client whose delete and/or update can be told to raise, by name.

    `failing_delete` maps a document name to the exception *instance* the
    delete should raise for it -- letting a test choose exactly which
    `FrappeError` subclass comes back, which is the point of the boundary
    these tests pin.
    """

    def __init__(self, failing_delete=None, failing_update=()):
        self.failing_delete: dict[str, Exception] = dict(failing_delete or {})
        self.failing_update = set(failing_update)
        self.deleted: list[str] = []
        self.updated: list[str] = []

    def delete(self, doctype: str, name: str):
        if name in self.failing_delete:
            raise self.failing_delete[name]
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


def test_a_delete_refused_with_link_exists_falls_back_to_disable() -> None:
    # ERPNext's literal "you can disable this Account instead" refusal.
    from stolon.frappe.errors import LinkExistsError

    root = live("Expenses - AB", "Expenses")
    a = live("A - AB", "A", parent="Expenses - AB")
    b = live("B - AB", "B", parent="Expenses - AB")
    plan = plan_prune([a, b], [root, a, b], set())
    client = FakeClient(failing_delete={"A - AB": LinkExistsError("still linked")})
    report = execute_prune(client, plan)
    assert "B - AB" in report.deleted
    assert report.disabled == ["A - AB"]
    assert report.failed == []
    assert client.updated == ["A - AB"]


def test_a_delete_refused_with_validation_error_falls_back_to_disable() -> None:
    # A generic business-rule refusal, distinct from both LinkExistsError
    # and the nested-set-specific ChildExistsError below.
    from stolon.frappe.errors import ValidationError

    root = live("Expenses - AB", "Expenses")
    a = live("A - AB", "A", parent="Expenses - AB")
    plan = plan_prune([a], [root, a], set())
    client = FakeClient(failing_delete={"A - AB": ValidationError("cannot be disabled as it is already set as ...")})
    report = execute_prune(client, plan)
    assert report.disabled == ["A - AB"]
    assert report.failed == []


def test_a_delete_refused_with_child_exists_falls_back_to_disable() -> None:
    # The regression this test pins: the real cascade. A child that can only
    # be disabled (LinkExistsError on its own delete) leaves its parent with
    # a live child node -- disabled, but still a node -- so the parent's own
    # delete then fails too, and not with LinkExistsError or ValidationError:
    # it is ChildExistsError (Frappe's NestedSetChildExistsError). A tuple
    # missing this class would silently stop falling back for the parent,
    # exactly what nearly shipped. Built directly from a PrunePlan, deepest
    # first, rather than via plan_prune: that function works from a
    # pre-execution snapshot and has no way to see the child's disable
    # happening mid-run -- this is deliberately execute_prune's own outcome.
    from stolon.frappe.errors import ChildExistsError, LinkExistsError

    group = live("Old Group - AB", "Old Group", parent="Expenses - AB", is_group=True)
    child = live("Old Leaf - AB", "Old Leaf", parent="Old Group - AB")
    plan = PrunePlan([DeleteAccount(child), DeleteAccount(group)], [])
    client = FakeClient(
        failing_delete={
            "Old Leaf - AB": LinkExistsError("still linked"),
            "Old Group - AB": ChildExistsError("Cannot delete Old Group - AB as it has child nodes"),
        }
    )
    report = execute_prune(client, plan)
    assert client.deleted == []
    assert report.disabled == ["Old Leaf - AB", "Old Group - AB"]
    assert report.failed == []


def test_a_delete_refused_with_rate_limit_propagates_and_does_not_disable() -> None:
    # The regression this boundary exists to pin: infrastructure trouble
    # must not be treated the same as a business refusal. A retry might
    # clear a rate limit; disabling the account instead would be wrong, and
    # `except FrappeError` would have done exactly that.
    from stolon.frappe.errors import RateLimitError

    root = live("Expenses - AB", "Expenses")
    a = live("A - AB", "A", parent="Expenses - AB")
    plan = plan_prune([a], [root, a], set())
    client = FakeClient(failing_delete={"A - AB": RateLimitError("slow down")})
    with pytest.raises(RateLimitError):
        execute_prune(client, plan)
    assert client.updated == []


def test_a_disable_refused_with_rate_limit_propagates_and_is_not_recorded_as_failed() -> None:
    # The disable path's own version of the delete-path regression above:
    # `except FrappeError` on the disable would turn a 429 into
    # report.failed, disagreeing with the module docstring's claim that
    # transient errors propagate. RateLimitError must come out of
    # execute_prune uncaught here too, not land in report.failed.
    from stolon.frappe.errors import LinkExistsError, RateLimitError

    class RateLimitedOnDisable:
        def __init__(self) -> None:
            self.deleted: list[str] = []
            self.updated: list[str] = []

        def delete(self, doctype: str, name: str):
            raise LinkExistsError("still linked")

        def update(self, doctype: str, name: str, doc: dict):
            raise RateLimitError("slow down")

    root = live("Expenses - AB", "Expenses")
    a = live("A - AB", "A", parent="Expenses - AB")
    plan = plan_prune([a], [root, a], set())
    client = RateLimitedOnDisable()
    with pytest.raises(RateLimitError):
        execute_prune(client, plan)
    assert client.deleted == []


def test_a_delete_that_fails_and_a_disable_that_also_fails_is_recorded_as_failed() -> None:
    from stolon.frappe.errors import LinkExistsError

    root = live("Expenses - AB", "Expenses")
    a = live("A - AB", "A", parent="Expenses - AB")
    plan = plan_prune([a], [root, a], set())
    client = FakeClient(failing_delete={"A - AB": LinkExistsError("still linked")}, failing_update={"A - AB"})
    report = execute_prune(client, plan)
    assert report.deleted == []
    assert report.disabled == []
    assert [name for name, _ in report.failed] == ["A - AB"]
    assert report.failed[0][1] == "cannot disable either"
