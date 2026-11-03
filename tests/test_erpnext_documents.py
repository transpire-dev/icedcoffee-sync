"""The ERPNext document shapes an integration writes, under the caller's namespace."""

from icedcoffee_sync.erpnext import documents as d
from icedcoffee_sync.erpnext.namespace import Namespace

ACME = Namespace(name="acme", label="Acme", site_header="X-Acme-Site")


def test_custom_field_is_named_the_way_frappe_names_it() -> None:
    field = d.DesiredCustomField(dt="Supplier", fieldname="acme_ref", label="Acme Ref", insert_after="supplier_name")
    assert field.document_name == "Supplier-acme_ref"
    doc = field.as_document()
    assert doc["dt"] == "Supplier" and doc["fieldname"] == "acme_ref"
    assert doc["read_only"] == 1 and doc["no_copy"] == 1
    assert "unique" not in doc


def test_every_pushed_document_carries_the_source_marker() -> None:
    supplier = d.DesiredSupplier(vendor_id="v1", supplier_name="Shimano")
    item = d.DesiredItem(item_code="CN-1", item_name="CHAIN", description="CHAIN")
    line = d.DesiredPurchaseOrderItem(item_code="CN-1", qty=2, schedule_date="2026-04-17")
    order = d.DesiredPurchaseOrder(
        order_id="o1",
        vendor_id="v1",
        order_number="12672682",
        transaction_date="2026-04-17",
        schedule_date="2026-04-17",
        items=(line,),
    )
    for doc in (
        supplier.as_document(namespace=ACME),
        item.as_document(namespace=ACME),
        order.as_document("Shimano", namespace=ACME),
    ):
        assert doc["acme_source"] == "acme"
    assert supplier.as_document(namespace=ACME)["acme_ref"] == "v1"
    assert order.as_document("Shimano", namespace=ACME)["acme_ref"] == "o1"
    assert order.as_document("Shimano", namespace=ACME)["acme_order_no"] == "12672682"


def test_the_custom_fields_are_named_and_labelled_for_the_namespace() -> None:
    fields = d.custom_fields(ACME)
    assert [(f.dt, f.fieldname, f.label) for f in fields] == [
        ("Supplier", "acme_source", "Acme Source"),
        ("Supplier", "acme_ref", "Acme Vendor ID"),
        ("Item", "acme_source", "Acme Source"),
        ("Purchase Order", "acme_source", "Acme Source"),
        ("Purchase Order", "acme_ref", "Acme Order ID"),
        ("Purchase Order", "acme_order_no", "Vendor Order Number"),
    ]
    # Each field the documents write into is one the site is given.
    written = {
        *d.DesiredSupplier(vendor_id="v1", supplier_name="Shimano").as_document(namespace=ACME),
    } - {"supplier_name", "disabled"}
    assert written <= {f.fieldname for f in fields if f.dt == "Supplier"}


def test_supplier_ref_field_is_one_of_the_custom_fields() -> None:
    assert d.supplier_ref_field(ACME) in d.custom_fields(ACME)
    assert d.supplier_ref_field(ACME).document_name == "Supplier-acme_ref"


def test_an_item_is_stock_maintained() -> None:
    # These are bicycle parts a shop holds in inventory, not services. ERPNext
    # writes no stock ledger entry for an item with Maintain Stock off, so a
    # Purchase Receipt against one moves nothing.
    item = d.DesiredItem(item_code="CN-1", item_name="CHAIN", description="CHAIN")
    assert item.as_document(namespace=ACME)["is_stock_item"] == 1
