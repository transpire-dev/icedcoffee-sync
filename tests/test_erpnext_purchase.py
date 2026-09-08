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
    """A site whose `list_page` answers from a scripted store the way Frappe v2 does.

    `edit` between pages moves a document's `modified`, which is the hazard
    the pager exists to survive.
    """

    def __init__(self, docs: Sequence[tuple[str, str]]) -> None:
        self.docs = dict(docs)  # name -> modified
        self.kwargs: list[dict[str, Any]] = []

    def edit(self, name: str, modified: str) -> None:
        self.docs[name] = modified

    def list_page(
        self, doctype: str, limit: int, start: int, **kwargs: Any
    ) -> tuple[list[dict[str, Any]], bool | None]:
        self.kwargs.append({"limit": limit, "start": start, **kwargs})
        rows = [{"name": n, "modified": m} for n, m in self.docs.items()]
        for field, op, value in kwargs["filters"]:
            if field == "docstatus":
                continue
            ops = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "=": lambda a, b: a == b}
            rows = [r for r in rows if ops[op](r[field], value)]
        key = (
            (lambda r: (r["modified"], r["name"]))
            if kwargs["order_by"].startswith("modified")
            else (lambda r: r["name"])
        )
        rows.sort(key=key)
        page = rows[start : start + limit]
        return page, len(rows) > start + limit


def _cursor_of(kw: dict[str, Any]) -> tuple[str, str | None] | None:
    """(`modified` op, value) of a request, for asserting how the pager moved."""
    for field, op, value in kw["filters"]:
        if field == "modified":
            return op, value
    return None


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
    # One short page: nothing more was asked for.
    assert len(site.kwargs) == 1


def test_a_first_run_with_no_cursor_filters_on_submitted_only() -> None:
    """Without a cursor the whole site is in scope, but drafts still are not."""
    site = _PagedSite([])
    assert list(p.list_submitted_purchase_orders(site, modified_after=None)) == []
    assert site.kwargs[0]["filters"] == [["docstatus", "in", [1, 2]]]


def test_a_company_filter_narrows_the_listing() -> None:
    site = _PagedSite([])
    assert list(p.list_submitted_purchase_orders(site, modified_after=None, company="Acmebike Cycles")) == []
    assert site.kwargs[0]["filters"] == [["docstatus", "in", [1, 2]], ["company", "=", "Acmebike Cycles"]]


def test_a_document_edited_between_pages_cannot_hide_an_unread_one() -> None:
    """Offset paging would skip PUR-ORD-3 here; key paging lists it."""
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
    assert rest == ["PUR-ORD-3", "PUR-ORD-4", "PUR-ORD-1"]
    # After the first page: the rest of the tie at its last `modified` (none),
    # then strictly after it; never an offset into a changed order.
    assert [_cursor_of(kw) for kw in site.kwargs[1:3]] == [
        ("=", "2026-08-01 10:00:01.000000"),
        (">", "2026-08-01 10:00:01.000000"),
    ]
    assert site.kwargs[1]["filters"][-1] == ["name", ">", "PUR-ORD-2"]
    assert all(kw["start"] == 0 for kw in site.kwargs)


def test_a_tie_wider_than_a_page_is_walked_by_name() -> None:
    same = "2026-08-01 10:00:00.000000"
    site = _PagedSite([(f"PUR-ORD-{i}", same) for i in range(5)] + [("PUR-ORD-9", "2026-08-01 10:00:05.000000")])
    rows = [r["name"] for r in p.list_submitted_purchase_orders(site, modified_after=None, page_size=2)]
    assert rows == [f"PUR-ORD-{i}" for i in range(5)] + ["PUR-ORD-9"]
    assert [_cursor_of(kw) for kw in site.kwargs] == [None, ("=", same), ("=", same), (">", same)]
    assert [kw["filters"][-1] for kw in site.kwargs[1:3]] == [["name", ">", "PUR-ORD-1"], ["name", ">", "PUR-ORD-3"]]


def test_a_server_that_caps_the_page_is_paged_by_its_flag_not_the_count() -> None:
    """A short page with `has_next_page: true` (a cap) keeps going; only the flag ends it."""

    class Capped(_PagedSite):
        def list_page(
            self, doctype: str, limit: int, start: int, **kwargs: Any
        ) -> tuple[list[dict[str, Any]], bool | None]:
            page, more = super().list_page(doctype, min(limit, 1), start, **kwargs)
            self.kwargs[-1]["limit"] = limit
            return page, more

    site = Capped([(f"PUR-ORD-{i}", f"2026-08-01 10:00:0{i}.000000") for i in range(3)])
    rows = [r["name"] for r in p.list_submitted_purchase_orders(site, modified_after=None, page_size=100)]
    assert rows == ["PUR-ORD-0", "PUR-ORD-1", "PUR-ORD-2"]


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
