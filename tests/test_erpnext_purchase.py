"""Reading a Purchase Order into a typed record, and the two fields we write back."""

import json
import pathlib
from typing import Any

from stolon.erpnext import purchase as p

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "purchase_order_submitted.json"


def _doc() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_a_submitted_purchase_order_reads_into_a_typed_record() -> None:
    po = p.purchase_order_from_document(_doc())
    assert po.name.startswith("PUR-ORD-")
    assert po.supplier == "SUP"
    assert po.company == "Acmebike Cycles" and po.currency == "USD"
    assert po.docstatus == 1 and po.is_submitted and not po.is_cancelled
    assert po.transpire_source == "transpire" and po.transpire_order_no == "0001ABCD"
    assert po.order_confirmation_no is None
    assert po.amended_from is None
    assert po.items and po.items[0].item_code == "IT0001" and po.items[0].qty == 1.0 and po.items[0].uom == "Nos"


def test_empty_strings_read_as_none() -> None:
    doc = _doc()
    doc["order_confirmation_no"] = ""
    assert p.purchase_order_from_document(doc).order_confirmation_no is None


def test_set_order_confirmation_updates_exactly_two_fields() -> None:
    class Site:
        def __init__(self) -> None:
            self.updates: list[tuple[str, str, dict[str, Any]]] = []

        def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
            self.updates.append((doctype, name, doc))
            return {"name": name, **doc}

    site = Site()
    p.set_order_confirmation(site, "PUR-ORD-1", number="0001ABCD", date="2026-08-02")
    assert site.updates == [
        ("Purchase Order", "PUR-ORD-1", {"order_confirmation_no": "0001ABCD", "order_confirmation_date": "2026-08-02"})
    ]


def test_listing_submitted_orders_pages_oldest_first_from_a_cursor() -> None:
    class Site:
        def __init__(self) -> None:
            self.kwargs: list[dict[str, Any]] = []

        def iter_all(self, doctype: str, **kwargs: Any):
            self.kwargs.append(kwargs)
            yield {"name": "PUR-ORD-1", "modified": "2026-08-01 10:00:00.000000"}

    site = Site()
    rows = list(p.list_submitted_purchase_orders(site, modified_after="2026-07-01 00:00:00"))
    assert rows == [{"name": "PUR-ORD-1", "modified": "2026-08-01 10:00:00.000000"}]
    kw = site.kwargs[0]
    # Inclusive: documents tied at the cursor timestamp are listed again rather
    # than lost to a run that stopped between them.
    assert kw["filters"] == [["docstatus", "=", 1], ["modified", ">=", "2026-07-01 00:00:00"]]
    assert kw["order_by"] == "modified asc"
    assert kw["fields"] == ["name", "modified"]


def test_a_first_run_with_no_cursor_filters_on_submitted_only() -> None:
    """Without a cursor the whole site is in scope, but drafts still are not."""

    class Site:
        def __init__(self) -> None:
            self.kwargs: list[dict[str, Any]] = []

        def iter_all(self, doctype: str, **kwargs: Any):
            self.kwargs.append(kwargs)
            return iter(())

    site = Site()
    assert list(p.list_submitted_purchase_orders(site, modified_after=None)) == []
    assert site.kwargs[0]["filters"] == [["docstatus", "=", 1]]


def test_a_draft_purchase_receipt_names_its_source_and_carries_no_docstatus() -> None:
    receipt = p.DesiredPurchaseReceipt(
        purchase_order="PUR-ORD-1",
        supplier="SUP",
        supplier_delivery_note="123456789012345",
        posting_date="2026-08-04",
        remarks="Drafted by Transpire from packing slip 123456789012345 received 2026-08-04. Review quantities before submitting.",
        items=(
            p.DesiredPurchaseReceiptItem(
                item_code="IT0001", qty=1, purchase_order="PUR-ORD-1", purchase_order_item="row-1", rate=0
            ),
        ),
    )
    doc = receipt.as_document()
    assert "docstatus" not in doc
    assert doc["supplier_delivery_note"] == "123456789012345"
    assert doc["items"][0] == {
        "item_code": "IT0001",
        "qty": 1,
        "uom": "Nos",
        "rate": 0,
        "purchase_order": "PUR-ORD-1",
        "purchase_order_item": "row-1",
    }
