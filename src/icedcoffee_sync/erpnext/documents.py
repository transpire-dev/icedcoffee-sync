"""The ERPNext documents an integration writes, as dataclasses with `as_document()`.

One definition for every writer, so two programs writing to the same site
under one `Namespace` never drift on a field name or a marker value.

Every document carries `<namespace>_source = <namespace>`, which is what keeps
a row the integration wrote distinguishable from a site's native data and from
anything a person types later.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from icedcoffee_sync.erpnext.namespace import Namespace

# Present on every ERPNext site out of the box, so the integration needs no
# site configuration of its own.
DEFAULT_ITEM_GROUP = "All Item Groups"
DEFAULT_UOM = "Nos"


@dataclasses.dataclass(frozen=True)
class DesiredCustomField:
    """One Custom Field the integration needs before it can key anything.

    `document_name` is deterministic (`<dt>-<fieldname>`), which is what makes
    ensuring these idempotent without a query on anything but the name.

    Deliberately NOT `unique`: a unique index over an existing doctype whose
    rows all carry an empty string for the new column is rejected by MariaDB
    on the second empty row, so marking the marker unique would fail on any
    site that already holds suppliers -- which is every site.
    """

    dt: str
    fieldname: str
    label: str
    insert_after: str
    fieldtype: str = "Data"

    @property
    def document_name(self) -> str:
        return f"{self.dt}-{self.fieldname}"

    def as_document(self) -> dict[str, Any]:
        return {
            "dt": self.dt,
            "fieldname": self.fieldname,
            "label": self.label,
            "fieldtype": self.fieldtype,
            "insert_after": self.insert_after,
            # Machine-owned: editable through the API, not through the form.
            "read_only": 1,
            # An amended document starts as a copy; carrying the marker onto
            # it would claim the integration wrote a row it has never seen.
            "no_copy": 1,
        }


def custom_fields(namespace: Namespace) -> tuple[DesiredCustomField, ...]:
    """Every Custom Field the documents below write into, named for `namespace`."""
    ns = namespace
    return (
        DesiredCustomField("Supplier", ns.source_field, f"{ns.label} Source", insert_after="supplier_name"),
        supplier_ref_field(ns),
        # An Item needs no ref field: its `item_code` IS the reference, and
        # Frappe names the document from it. The source marker is what a site's
        # own demo Items lack, which is what makes a reused one recognisable.
        DesiredCustomField("Item", ns.source_field, f"{ns.label} Source", insert_after="item_name"),
        DesiredCustomField("Purchase Order", ns.source_field, f"{ns.label} Source", insert_after="supplier"),
        DesiredCustomField("Purchase Order", ns.ref_field, f"{ns.label} Order ID", insert_after=ns.source_field),
        DesiredCustomField("Purchase Order", ns.order_no_field, "Vendor Order Number", insert_after=ns.ref_field),
    )


def supplier_ref_field(namespace: Namespace) -> DesiredCustomField:
    """The Supplier's ref field on its own: what links a site's existing Supplier to the caller's vendor id."""
    return DesiredCustomField(
        "Supplier", namespace.ref_field, f"{namespace.label} Vendor ID", insert_after=namespace.source_field
    )


@dataclasses.dataclass(frozen=True, kw_only=True)
class DesiredSupplier:
    """One Supplier, keyed by the tenant's vendor id rather than by its name."""

    vendor_id: str
    supplier_name: str
    disabled: bool = False

    def as_document(self, *, namespace: Namespace) -> dict[str, Any]:
        return {
            "supplier_name": self.supplier_name,
            "disabled": 1 if self.disabled else 0,
            namespace.source_field: namespace.marker,
            namespace.ref_field: self.vendor_id,
        }


@dataclasses.dataclass(frozen=True)
class DesiredItem:
    """One Item, named by a deterministic slug so creating it is idempotent.

    Stock-maintained: these are parts a shop holds in inventory, and ERPNext
    writes no stock ledger entry for an item with Maintain Stock off -- so a
    Purchase Receipt against a non-stock item moves nothing, which is the half
    of the demo that matters. The warehouse and valuation rate this drags in
    are answered by the site's company defaults and by the line's own rate.
    """

    item_code: str
    item_name: str
    description: str | None

    def as_document(self, *, namespace: Namespace) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "description": self.description or self.item_name,
            "item_group": DEFAULT_ITEM_GROUP,
            "stock_uom": DEFAULT_UOM,
            "is_stock_item": 1,
            namespace.source_field: namespace.marker,
        }


@dataclasses.dataclass(frozen=True, kw_only=True)
class DesiredPurchaseOrderItem:
    """One Purchase Order line.

    `rate` is the unit price in the site's company currency, or 0 when the
    caller has no price it can vouch for: the integration never invents one.
    """

    item_code: str
    item_name: str = ""
    description: str | None = None
    qty: int
    schedule_date: str
    rate: float = 0

    def as_document(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "description": self.description or self.item_name,
            "qty": self.qty,
            "rate": self.rate,
            "uom": DEFAULT_UOM,
            "conversion_factor": 1,
            "schedule_date": self.schedule_date,
        }


@dataclasses.dataclass(frozen=True)
class DesiredPurchaseOrder:
    """One Purchase Order, minus the Supplier name only the site can assign."""

    order_id: str
    vendor_id: str
    order_number: str | None
    transaction_date: str
    schedule_date: str
    items: tuple[DesiredPurchaseOrderItem, ...]

    def as_document(self, supplier: str, *, namespace: Namespace) -> dict[str, Any]:
        return {
            "supplier": supplier,
            "transaction_date": self.transaction_date,
            "schedule_date": self.schedule_date,
            "items": [item.as_document() for item in self.items],
            namespace.source_field: namespace.marker,
            namespace.ref_field: self.order_id,
            namespace.order_no_field: self.order_number or "",
        }
