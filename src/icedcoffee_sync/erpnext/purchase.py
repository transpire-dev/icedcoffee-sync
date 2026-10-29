"""Purchase Orders as ERPNext holds them, and the documents we write against one.

Reads are typed: the connector needs a stable shape to turn into observations,
and a dict from Frappe carries "" where we mean None. Writes are narrow: the
two `allow_on_submit` confirmation fields (verified on the v16 Purchase Order
doctype), and a draft Purchase Receipt. Nothing here submits.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any, Protocol

DOCTYPE_PURCHASE_ORDER = "Purchase Order"
DOCTYPE_PURCHASE_RECEIPT = "Purchase Receipt"
DEFAULT_UOM = "Nos"


class _Getter(Protocol):
    def get(self, doctype: str, name: str) -> Any: ...


class _Lister(Protocol):
    def list_page(
        self, doctype: str, limit: int, start: int, **kwargs: Any
    ) -> tuple[list[dict[str, Any]], bool | None]: ...


class _Updater(Protocol):
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _num(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


@dataclasses.dataclass(frozen=True)
class PurchaseOrderItem:
    """One row of a Purchase Order's `items` table."""

    name: str
    idx: int
    item_code: str
    item_name: str | None
    description: str | None
    qty: float
    rate: float
    amount: float
    uom: str
    schedule_date: str | None


@dataclasses.dataclass(frozen=True)
class PurchaseOrder:
    """A Purchase Order as read from the site."""

    name: str
    supplier: str
    supplier_name: str | None
    company: str | None
    currency: str | None
    transaction_date: str | None
    schedule_date: str | None
    docstatus: int
    status: str | None
    grand_total: float
    order_confirmation_no: str | None
    order_confirmation_date: str | None
    amended_from: str | None
    modified: str
    transpire_source: str | None
    transpire_ref: str | None
    transpire_order_no: str | None
    items: tuple[PurchaseOrderItem, ...]

    @property
    def is_submitted(self) -> bool:
        return self.docstatus == 1

    @property
    def is_cancelled(self) -> bool:
        return self.docstatus == 2


def purchase_order_from_document(doc: dict[str, Any]) -> PurchaseOrder:
    """Type a raw `Purchase Order` document. Pure."""
    items = tuple(
        PurchaseOrderItem(
            name=str(row.get("name", "")),
            idx=int(row.get("idx", 0)),
            item_code=str(row["item_code"]),
            item_name=_text(row.get("item_name")),
            description=_text(row.get("description")),
            qty=_num(row.get("qty")),
            rate=_num(row.get("rate")),
            amount=_num(row.get("amount")),
            uom=_text(row.get("uom")) or DEFAULT_UOM,
            schedule_date=_text(row.get("schedule_date")),
        )
        for row in doc.get("items") or []
    )
    return PurchaseOrder(
        name=str(doc["name"]),
        supplier=str(doc["supplier"]),
        supplier_name=_text(doc.get("supplier_name")),
        company=_text(doc.get("company")),
        currency=_text(doc.get("currency")),
        transaction_date=_text(doc.get("transaction_date")),
        schedule_date=_text(doc.get("schedule_date")),
        docstatus=int(doc.get("docstatus", 0)),
        status=_text(doc.get("status")),
        grand_total=_num(doc.get("grand_total")),
        order_confirmation_no=_text(doc.get("order_confirmation_no")),
        order_confirmation_date=_text(doc.get("order_confirmation_date")),
        amended_from=_text(doc.get("amended_from")),
        modified=str(doc.get("modified", "")),
        transpire_source=_text(doc.get("transpire_source")),
        transpire_ref=_text(doc.get("transpire_ref")),
        transpire_order_no=_text(doc.get("transpire_order_no")),
        items=items,
    )


def read_purchase_order(client: _Getter, name: str) -> PurchaseOrder:
    """Fetch and type one Purchase Order."""
    return purchase_order_from_document(client.get(DOCTYPE_PURCHASE_ORDER, name))


def list_submitted_purchase_orders(
    client: _Lister, *, modified_after: str | None, page_size: int = 100, company: str | None = None
) -> Iterator[dict[str, Any]]:
    """`{"name", "modified"}` for every submitted or cancelled PO past the cursor, oldest first.

    `modified_after` is Frappe's own timestamp text (`YYYY-MM-DD HH:MM:SS[.ffffff]`),
    compared server-side, so the cursor a caller stores is exactly what the
    site last returned. The document at the cursor itself is listed again;
    see the comment on the filter.

    `company` narrows to one Frappe Company (a tenant's site may run several);
    omitted, every company on the site is in scope.

    Pages by key, never by offset. `modified` is what the listing is ordered
    by, and it moves: a document edited between two pages sorts later than it
    did, and with offsets the unread document that slid into its old slot
    would be skipped, permanently, once the caller stored the cursor. Each
    page instead continues from the last `(modified, name)` yielded, in two
    steps because Frappe's list filters only AND together: first the rest of
    the documents tied at that `modified`, by `name`, then everything after
    it. An edit only ever moves a document later, so it can be listed twice
    (every caller keys its source id on `modified`, so a repeat is
    idempotent) but not lost.
    """
    cursor = modified_after
    last_name: str | None = None
    while True:
        if last_name is not None:
            # The rest of the tie at `cursor`. `name` is immutable, so this
            # walk is stable however the documents are edited meanwhile.
            while True:
                tie = [["modified", "=", cursor], ["name", ">", last_name]]
                rows, more = _page(client, tie, order_by="name asc", page_size=page_size, company=company)
                if not rows:
                    break
                yield from rows
                last_name = rows[-1]["name"]
                if _last_page(rows, more, page_size):
                    break
        after: list[list[Any]] = []
        if cursor is not None:
            # Inclusive on the caller's own cursor, on purpose: several
            # documents can share one `modified`, and a run that stopped
            # between two of them would otherwise never see the rest.
            # Re-reading the document at the cursor is idempotent for every
            # caller. Strict once the tie has been walked above.
            after = [["modified", ">" if last_name is not None else ">=", cursor]]
        rows, more = _page(client, after, order_by="modified asc, name asc", page_size=page_size, company=company)
        if not rows:
            return
        yield from rows
        cursor, last_name = rows[-1]["modified"], rows[-1]["name"]
        if _last_page(rows, more, page_size):
            return


def _page(
    client: _Lister, filters: list[list[Any]], *, order_by: str, page_size: int, company: str | None = None
) -> tuple[list[dict[str, Any]], bool | None]:
    # Submitted and cancelled, never drafts: a cancellation whose Webhook was
    # lost is exactly what the reconcile poll exists to recover.
    base: list[list[Any]] = [["docstatus", "in", [1, 2]]]
    if company:
        base.append(["company", "=", company])
    return client.list_page(
        DOCTYPE_PURCHASE_ORDER,
        page_size,
        0,
        fields=["name", "modified"],
        filters=[*base, *filters],
        order_by=order_by,
    )


def _last_page(rows: list[dict[str, Any]], more: bool | None, page_size: int) -> bool:
    """Trust the server's flag; without one (v1), a short page is the end."""
    return more is False or (more is None and len(rows) < page_size)


def set_order_confirmation(client: _Updater, name: str, *, number: str, date: str) -> dict[str, Any]:
    """Set the vendor's order number and date on a submitted PO. Both fields allow it."""
    return client.update(
        DOCTYPE_PURCHASE_ORDER, name, {"order_confirmation_no": number, "order_confirmation_date": date}
    )


@dataclasses.dataclass(frozen=True)
class DesiredPurchaseReceiptItem:
    """One received line, linked back to the PO row it fulfils."""

    item_code: str
    qty: float
    purchase_order: str
    purchase_order_item: str
    rate: float
    uom: str = DEFAULT_UOM

    def as_document(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "qty": self.qty,
            "uom": self.uom,
            "rate": self.rate,
            "purchase_order": self.purchase_order,
            "purchase_order_item": self.purchase_order_item,
        }


@dataclasses.dataclass(frozen=True)
class DesiredPurchaseReceipt:
    """A draft Purchase Receipt against one PO. Never carries a docstatus."""

    purchase_order: str
    supplier: str
    supplier_delivery_note: str
    posting_date: str
    remarks: str
    items: tuple[DesiredPurchaseReceiptItem, ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "supplier": self.supplier,
            "posting_date": self.posting_date,
            "supplier_delivery_note": self.supplier_delivery_note,
            "remarks": self.remarks,
            "items": [item.as_document() for item in self.items],
        }
