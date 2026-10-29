"""Get-or-insert by document name, the idempotent shape of every provisioning step."""

from typing import Any

from icedcoffee_sync.erpnext.ensure import ensure_by_name
from icedcoffee_sync.frappe.errors import DoesNotExistError


class FakeSite:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.inserts: list[tuple[str, dict[str, Any]]] = []

    def get(self, doctype: str, name: str) -> Any:
        try:
            return self.docs[(doctype, name)]
        except KeyError:
            raise DoesNotExistError(f"{doctype} {name} not found") from None

    def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
        self.inserts.append((doctype, doc))
        stored = {**doc, "name": doc.get("name") or doc.get("year") or "assigned"}
        self.docs[(doctype, stored["name"])] = stored
        return stored


def test_a_missing_document_is_inserted_once() -> None:
    site = FakeSite()
    assert ensure_by_name(site, "Webhook", "hook-1", {"name": "hook-1", "request_url": "https://x"}) is True
    assert ensure_by_name(site, "Webhook", "hook-1", {"name": "hook-1", "request_url": "https://x"}) is False
    assert len(site.inserts) == 1


def test_an_existing_document_is_left_exactly_as_it_is() -> None:
    site = FakeSite()
    site.docs[("Webhook", "hook-1")] = {"name": "hook-1", "request_url": "https://old"}
    assert ensure_by_name(site, "Webhook", "hook-1", {"name": "hook-1", "request_url": "https://new"}) is False
    assert site.docs[("Webhook", "hook-1")]["request_url"] == "https://old"
