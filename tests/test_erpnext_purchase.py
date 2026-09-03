"""Reading a Purchase Order into a typed record, and the two fields we write back."""

import json
import pathlib
from collections.abc import Sequence
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


class _PagedSite:
    """A site whose listing answers each request from a scripted store, in `modified, name` order.

    `edit` between pages moves a document's `modified`, which is the hazard
    the pager exists to survive.
    """

    def __init__(self, docs: Sequence[tuple[str, str]]) -> None:
        self.docs = dict(docs)  # name -> modified
        self.kwargs: list[dict[str, Any]] = []

    def edit(self, name: str, modified: str) -> None:
        self.docs[name] = modified

    def list(self, doctype: str, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        cursor = next((f[2] for f in kwargs["filters"] if f[0] == "modified"), None)
        rows = sorted(((m, n) for n, m in self.docs.items() if cursor is None or m >= cursor))
        page = rows[kwargs["start"] : kwargs["start"] + kwargs["limit"]]
        return [{"name": n, "modified": m} for m, n in page]


def test_listing_submitted_orders_pages_oldest_first_from_a_cursor() -> None:
    site = _PagedSite([("PUR-ORD-0", "2026-06-01 09:00:00.000000"), ("PUR-ORD-1", "2026-08-01 10:00:00.000000")])
    rows = list(p.list_submitted_purchase_orders(site, modified_after="2026-07-01 00:00:00"))
    assert rows == [{"name": "PUR-ORD-1", "modified": "2026-08-01 10:00:00.000000"}]
    kw = site.kwargs[0]
    # Inclusive: documents tied at the cursor timestamp are listed again rather
    # than lost to a run that stopped between them.
    assert kw["filters"] == [["docstatus", "in", [1, 2]], ["modified", ">=", "2026-07-01 00:00:00"]]
    assert kw["order_by"] == "modified asc, name asc"
    assert kw["fields"] == ["name", "modified"]
    assert (kw["limit"], kw["start"]) == (100, 0)


def test_a_first_run_with_no_cursor_filters_on_submitted_only() -> None:
    """Without a cursor the whole site is in scope, but drafts still are not."""
    site = _PagedSite([])
    assert list(p.list_submitted_purchase_orders(site, modified_after=None)) == []
    assert site.kwargs[0]["filters"] == [["docstatus", "in", [1, 2]]]


def test_a_document_edited_between_pages_cannot_hide_an_unread_one() -> None:
    """Offset paging would skip PUR-ORD-3 here; cursor paging lists it."""
    site = _PagedSite([
        ("PUR-ORD-1", "2026-08-01 10:00:00.000000"),
        ("PUR-ORD-2", "2026-08-01 10:00:01.000000"),
        ("PUR-ORD-3", "2026-08-01 10:00:02.000000"),
        ("PUR-ORD-4", "2026-08-01 10:00:03.000000"),
    ])
    listing = p.list_submitted_purchase_orders(site, modified_after=None, page_size=2)
    first = [next(listing), next(listing)]
    assert [r["name"] for r in first] == ["PUR-ORD-1", "PUR-ORD-2"]
    # PUR-ORD-1 is edited after page one was read: it now sorts last.
    site.edit("PUR-ORD-1", "2026-08-01 10:00:09.000000")
    rest = [r["name"] for r in listing]
    assert "PUR-ORD-3" in rest and "PUR-ORD-4" in rest
    # The edited document is listed again, later; a caller tolerates the repeat.
    assert rest[-1] == "PUR-ORD-1"
    # The second page re-filters from the last `modified` seen, at offset 0,
    # rather than asking for offset 2 of an order that has since changed.
    assert site.kwargs[1]["start"] == 0
    assert site.kwargs[1]["filters"][-1] == ["modified", ">=", "2026-08-01 10:00:01.000000"]


def test_a_page_full_of_ties_advances_by_offset_within_the_timestamp() -> None:
    same = "2026-08-01 10:00:00.000000"
    site = _PagedSite([(f"PUR-ORD-{i}", same) for i in range(5)] + [("PUR-ORD-9", "2026-08-01 10:00:05.000000")])
    rows = [r["name"] for r in p.list_submitted_purchase_orders(site, modified_after=None, page_size=2)]
    assert rows == [f"PUR-ORD-{i}" for i in range(5)] + ["PUR-ORD-9"]
    # A cursor move re-reads the tie it ended on (offset 0, already-yielded
    # names skipped); within a tie the offset walks forward; the next cursor
    # move resets it.
    pages = [(kw["start"], next((f[2] for f in kw["filters"] if f[0] == "modified"), None)) for kw in site.kwargs]
    assert pages[:5] == [(0, None), (0, same), (2, same), (4, same), (0, "2026-08-01 10:00:05.000000")]


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
