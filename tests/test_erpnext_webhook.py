"""The Frappe Webhook documents that ring phloem's doorbell."""

import json

from stolon.erpnext import webhook as w

SECRET = "7dc9c9544e17e1867efde0506bca3dacaaa5d11f661560591bc1acf0924e8c86"


def test_three_purchase_order_webhooks_named_deterministically() -> None:
    hooks = w.purchase_order_webhooks(site_id="site-1", request_url="https://gw.example/frappe", secret=SECRET)
    assert [h.name for h in hooks] == [
        "transpire-site-1-purchase-order-on_submit",
        "transpire-site-1-purchase-order-on_update_after_submit",
        "transpire-site-1-purchase-order-on_cancel",
    ]


def test_the_document_uses_the_v16_field_names_and_a_two_field_json_template() -> None:
    hook = w.purchase_order_webhooks(site_id="site-1", request_url="https://gw.example/frappe", secret=SECRET)[0]
    doc = hook.as_document()
    assert doc["name"] == hook.name
    assert doc["webhook_doctype"] == "Purchase Order"
    assert doc["webhook_docevent"] == "on_submit"
    assert doc["request_url"] == "https://gw.example/frappe"
    assert doc["request_method"] == "POST"
    assert doc["request_structure"] == "JSON"
    assert doc["enable_security"] == 1 and doc["webhook_secret"] == SECRET
    assert doc["enabled"] == 1 and doc["timeout"] == 10
    assert {h["key"]: h["value"] for h in doc["webhook_headers"]} == {
        "Content-Type": "application/json",
        "X-Transpire-Site": "site-1",
    }
    # The template is a doorbell: the document's identity and nothing else.
    rendered = doc["webhook_json"].replace("{{ doc.doctype }}", "Purchase Order").replace("{{ doc.name }}", "PUR-ORD-1")
    assert json.loads(rendered) == {"doctype": "Purchase Order", "name": "PUR-ORD-1"}


def test_the_secret_never_appears_in_the_repr() -> None:
    hook = w.purchase_order_webhooks(site_id="site-1", request_url="https://gw.example/frappe", secret=SECRET)[0]
    assert SECRET not in repr(hook)


def test_ensure_creates_the_missing_ones_and_reasserts_the_rest() -> None:
    from stolon.frappe.errors import DoesNotExistError

    class Site:
        def __init__(self) -> None:
            self.have = {"transpire-site-1-purchase-order-on_submit"}
            self.inserted: list[str] = []
            self.updated: list[tuple[str, dict]] = []

        def get(self, doctype: str, name: str) -> dict:
            if name in self.have:
                return {"name": name}
            raise DoesNotExistError("missing")

        def insert(self, doctype: str, doc: dict) -> dict:
            self.inserted.append(doc["name"])
            self.have.add(doc["name"])
            return doc

        def update(self, doctype: str, name: str, doc: dict) -> dict:
            self.updated.append((name, doc))
            return {"name": name, **doc}

    site = Site()
    hooks = w.purchase_order_webhooks(site_id="site-1", request_url="https://gw.example/frappe", secret=SECRET)
    first = w.ensure_webhooks(site, hooks)
    assert first.created == [
        "transpire-site-1-purchase-order-on_update_after_submit",
        "transpire-site-1-purchase-order-on_cancel",
    ]
    assert first.updated == ["transpire-site-1-purchase-order-on_submit"]
    # The existing one was brought back to the desired document, secret included,
    # and its name was not sent as a field to change.
    _name, doc = site.updated[0]
    assert doc["request_url"] == "https://gw.example/frappe" and doc["webhook_secret"] == SECRET
    assert "name" not in doc

    rotated = w.purchase_order_webhooks(site_id="site-1", request_url="https://gw2.example/frappe", secret="y" * 64)
    second = w.ensure_webhooks(site, rotated)
    assert second.created == [] and len(second.updated) == 3
    assert all(doc["request_url"] == "https://gw2.example/frappe" for _, doc in site.updated[1:])
