"""The ERPNext documents Transpire writes, as dataclasses with `as_document()`.

Shared by `graft` (which pushes a tenant's curated data onto a demo site) and
phloem's connector (which stamps identity markers onto a customer's site and
drafts documents there). One definition, so the two never drift on a field
name or a marker value.

Every document carries `transpire_source = transpire`, which is what keeps a
row we wrote distinguishable from a site's native data and from anything a
person types later.
"""

from __future__ import annotations

import dataclasses
from typing import Any

# What every document this connector writes is stamped with, so a pushed row
# stays distinguishable from the site's own ERPNext demo data and from
# anything a person types in later.
SOURCE_MARKER = "transpire"

FIELD_SOURCE = "transpire_source"
FIELD_REF = "transpire_ref"
FIELD_ORDER_NO = "transpire_order_no"

# Present on every ERPNext site out of the box, so the connector needs no
# site configuration of its own.
DEFAULT_ITEM_GROUP = "All Item Groups"
DEFAULT_UOM = "Nos"


@dataclasses.dataclass(frozen=True)
class DesiredCustomField:
    """One Custom Field the connector needs before it can key anything.

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
            # it would claim Transpire wrote a row it has never seen.
            "no_copy": 1,
        }


CUSTOM_FIELDS: tuple[DesiredCustomField, ...] = (
    DesiredCustomField("Supplier", FIELD_SOURCE, "Transpire Source", insert_after="supplier_name"),
    DesiredCustomField("Supplier", FIELD_REF, "Transpire Vendor ID", insert_after=FIELD_SOURCE),
    # An Item needs no `transpire_ref`: its `item_code` IS the reference, and
    # Frappe names the document from it. The source marker is what a site's own
    # demo Items lack, which is what makes a reused one recognisable.
    DesiredCustomField("Item", FIELD_SOURCE, "Transpire Source", insert_after="item_name"),
    DesiredCustomField("Purchase Order", FIELD_SOURCE, "Transpire Source", insert_after="supplier"),
    DesiredCustomField("Purchase Order", FIELD_REF, "Transpire Order ID", insert_after=FIELD_SOURCE),
    DesiredCustomField("Purchase Order", FIELD_ORDER_NO, "Vendor Order Number", insert_after=FIELD_REF),
)

# The one marker phloem's onboarding stamps onto a customer's Suppliers.
SUPPLIER_REF_FIELD = next(f for f in CUSTOM_FIELDS if f.dt == "Supplier" and f.fieldname == FIELD_REF)


@dataclasses.dataclass(frozen=True, kw_only=True)
class DesiredSupplier:
    """One Supplier, keyed by the tenant's vendor id rather than by its name."""

    vendor_id: str
    supplier_name: str
    disabled: bool = False

    def as_document(self) -> dict[str, Any]:
        return {
            "supplier_name": self.supplier_name,
            "disabled": 1 if self.disabled else 0,
            FIELD_SOURCE: SOURCE_MARKER,
            FIELD_REF: self.vendor_id,
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

    def as_document(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "description": self.description or self.item_name,
            "item_group": DEFAULT_ITEM_GROUP,
            "stock_uom": DEFAULT_UOM,
            "is_stock_item": 1,
            FIELD_SOURCE: SOURCE_MARKER,
        }


@dataclasses.dataclass(frozen=True, kw_only=True)
class DesiredPurchaseOrderItem:
    """One Purchase Order line.

    `rate` is the unit price in the site's company currency, or 0 when the
    caller has no price it can vouch for: the connector never invents one.
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

    def as_document(self, supplier: str) -> dict[str, Any]:
        return {
            "supplier": supplier,
            "transaction_date": self.transaction_date,
            "schedule_date": self.schedule_date,
            "items": [item.as_document() for item in self.items],
            FIELD_SOURCE: SOURCE_MARKER,
            FIELD_REF: self.order_id,
            FIELD_ORDER_NO: self.order_number or "",
        }
