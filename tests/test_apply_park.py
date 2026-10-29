"""Park a survivor's number out of the chart's way -- never delete it here."""

from __future__ import annotations

from typing import Any

from icedcoffee_sync.apply.model import LiveAccount
from icedcoffee_sync.apply.park import ParkedNumber, park_colliding_numbers, parking_candidates, unresolved_collisions
from icedcoffee_sync.apply.prune import PruneReason, PruneRefusal, PruneReport


def survivor(document_name: str, number: str, name: str, parent: str | None = "Expenses - T") -> LiveAccount:
    return LiveAccount(document_name, number, name, parent, False, "Expense", None, False)


class RecordingClient:
    """Records every renumber call; answers with a configurable name per `name` param."""

    def __init__(self, responses: dict[str, str | None] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = dict(responses or {})

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(dict(params or {}))
        return self._responses.get((params or {})["name"])


def test_a_colliding_survivor_is_parked_with_a_9_prefix() -> None:
    client = RecordingClient({"4110 - Sales - T": "94110 - Sales - T"})
    account = survivor("4110 - Sales - T", "4110", "Sales")
    parked = park_colliding_numbers(client, [account], frozenset({"4110"}), taken=frozenset())
    assert parked == [ParkedNumber("4110 - Sales - T", "4110", "94110", "94110 - Sales - T")]
    assert client.calls == [{"name": "4110 - Sales - T", "account_name": "Sales", "account_number": "94110"}]


def test_a_non_colliding_survivor_is_untouched() -> None:
    client = RecordingClient()
    account = survivor("9999 - Odd Account - T", "9999", "Odd Account")
    parked = park_colliding_numbers(client, [account], frozenset({"4110"}), taken=frozenset())
    assert parked == []
    assert client.calls == []


def test_a_9_prefixed_number_already_taken_gets_double_prefixed() -> None:
    client = RecordingClient({"4110 - Sales - T": "994110 - Sales - T"})
    account = survivor("4110 - Sales - T", "4110", "Sales")
    parked = park_colliding_numbers(client, [account], frozenset({"4110"}), taken=frozenset({"94110"}))
    assert parked == [ParkedNumber("4110 - Sales - T", "4110", "994110", "994110 - Sales - T")]
    assert client.calls == [{"name": "4110 - Sales - T", "account_name": "Sales", "account_number": "994110"}]


def test_a_9_prefixed_number_already_chosen_by_an_earlier_survivor_is_avoided() -> None:
    # Two live accounts sharing the same chart-colliding number -- the
    client = RecordingClient({
        "4110 - Sales - T": "94110 - Sales - T",
        "4110 - Sales Duplicate - T": "994110 - Sales Duplicate - T",
    })
    first = survivor("4110 - Sales - T", "4110", "Sales")
    second = survivor("4110 - Sales Duplicate - T", "4110", "Sales Duplicate")
    parked = park_colliding_numbers(client, [first, second], frozenset({"4110"}), taken=frozenset())
    assert [p.parked_number for p in parked] == ["94110", "994110"]


def test_a_root_is_never_parked() -> None:
    client = RecordingClient()
    root = survivor("4000 - Income - T", "4000", "Income", parent=None)
    parked = park_colliding_numbers(client, [root], frozenset({"4000"}), taken=frozenset())
    assert parked == []
    assert client.calls == []


def test_the_account_name_is_carried_unchanged() -> None:
    client = RecordingClient({"5218 - Old Name - T": "95218 - Old Name - T"})
    account = survivor("5218 - Old Name - T", "5218", "Old Name")
    park_colliding_numbers(client, [account], frozenset({"5218"}), taken=frozenset())
    assert client.calls[0]["account_name"] == "Old Name"


def test_the_fallback_name_is_used_when_the_server_returns_nothing() -> None:
    client = RecordingClient({"4110 - Sales - T": None})
    account = survivor("4110 - Sales - T", "4110", "Sales")
    parked = park_colliding_numbers(client, [account], frozenset({"4110"}), taken=frozenset())
    assert parked[0].new_document_name == "94110 - Sales - T"


def test_parking_candidates_includes_disabled_already_disabled_and_failed() -> None:
    disabled = survivor("D - T", "10", "D")
    already_disabled = survivor("AD - T", "20", "AD")
    failed = survivor("F - T", "30", "F")
    report = PruneReport(disabled=["D - T"], already_disabled=["AD - T"], failed=[("F - T", "boom")])
    candidates, gl_excluded = parking_candidates([disabled, already_disabled, failed], report, [])
    assert {a.document_name for a in candidates} == {"D - T", "AD - T", "F - T"}
    assert gl_excluded == []


def test_parking_candidates_includes_a_non_gl_refusal() -> None:
    child_blocked = survivor("C - T", "40", "C")
    refusal = PruneRefusal(child_blocked, PruneReason.HAS_LIVE_CHILD, "still has a live child")
    candidates, gl_excluded = parking_candidates([child_blocked], PruneReport(), [refusal])
    assert candidates == [child_blocked]
    assert gl_excluded == []


def test_parking_candidates_excludes_and_separately_reports_a_gl_refusal() -> None:
    busy = survivor("B - T", "50", "B")
    refusal = PruneRefusal(busy, PruneReason.HAS_GL_ENTRIES, "carries ledger entries")
    candidates, gl_excluded = parking_candidates([busy], PruneReport(), [refusal])
    assert candidates == []
    assert gl_excluded == [busy]


def test_parking_candidates_ignores_a_name_not_present_in_extras() -> None:
    report = PruneReport(disabled=["Missing - T"])
    candidates, gl_excluded = parking_candidates([], report, [])
    assert candidates == []
    assert gl_excluded == []


def test_unresolved_collisions_names_a_non_root_still_on_a_chart_number() -> None:
    busy = survivor("B - T", "50", "B")
    assert unresolved_collisions([busy], frozenset({"50"})) == ["B - T"]


def test_unresolved_collisions_ignores_a_root() -> None:
    root = survivor("Income - T", "4000", "Income", parent=None)
    assert unresolved_collisions([root], frozenset({"4000"})) == []


def test_unresolved_collisions_ignores_a_number_the_chart_does_not_need() -> None:
    idle = survivor("Idle - T", "60", "Idle")
    assert unresolved_collisions([idle], frozenset({"50"})) == []
