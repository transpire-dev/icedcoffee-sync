"""Park a survivor's number out of the chart's way -- never delete it here."""

from __future__ import annotations

from typing import Any

from stolon.apply.model import LiveAccount
from stolon.apply.park import ParkedNumber, park_colliding_numbers


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
    # second must not park at the same number the first just claimed.
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
