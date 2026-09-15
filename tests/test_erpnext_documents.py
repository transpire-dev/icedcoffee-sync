"""The ERPNext document shapes shared by graft and phloem."""

from stolon.erpnext import documents as d


def test_custom_field_is_named_the_way_frappe_names_it() -> None:
    field = d.DesiredCustomField(
        dt="Supplier", fieldname="transpire_ref", label="Transpire Ref", insert_after="supplier_name"
    )
    assert field.document_name == "Supplier-transpire_ref"
    doc = field.as_document()
    assert doc["dt"] == "Supplier" and doc["fieldname"] == "transpire_ref"
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
    for doc in (supplier.as_document(), item.as_document(), order.as_document("Shimano")):
        assert doc[d.FIELD_SOURCE] == d.SOURCE_MARKER


def test_supplier_ref_field_is_the_one_graft_creates() -> None:
    assert d.SUPPLIER_REF_FIELD in d.CUSTOM_FIELDS
    assert d.SUPPLIER_REF_FIELD.document_name == "Supplier-transpire_ref"


def test_an_item_is_stock_maintained() -> None:
    # These are bicycle parts a shop holds in inventory, not services. ERPNext
    # writes no stock ledger entry for an item with Maintain Stock off, so a
    # Purchase Receipt against one moves nothing.
    assert d.DesiredItem(item_code="CN-1", item_name="CHAIN", description="CHAIN").as_document()["is_stock_item"] == 1
