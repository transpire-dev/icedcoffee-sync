"""Frappe `Webhook` documents, provisioned so nobody transcribes a form.

A Webhook fires from `Document.run_method`, so a Purchase Order submitted from
the desk, the REST API, or a script all ring the same bell. The body is a
two-field template naming the document; phloem fetches the rest. The site id
rides in a header so the gateway can tell which site's secret signs the
request without a per-site route.

Field names are ERPNext v16's (`frappe/integrations/doctype/webhook/webhook.json`
on version-16-hotfix). Frappe names a Webhook by prompt, so the caller sets
`name`, which is what makes provisioning idempotent.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any, Protocol

from stolon.erpnext.ensure import ensure_by_name

DOCTYPE_WEBHOOK = "Webhook"
PURCHASE_ORDER = "Purchase Order"
PURCHASE_ORDER_EVENTS: tuple[str, ...] = ("on_submit", "on_update_after_submit", "on_cancel")

SITE_HEADER = "X-Transpire-Site"
TIMEOUT_SECONDS = 10

# A doorbell, not the data. Frappe renders this Jinja against the document.
DOORBELL_TEMPLATE = '{"doctype": "{{ doc.doctype }}", "name": "{{ doc.name }}"}'


class _Writer(Protocol):
    def get(self, doctype: str, name: str) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...


def webhook_name(site_id: str, doctype: str, event: str) -> str:
    """`transpire-<site>-<doctype slug>-<event>`; deterministic, so ensure is get-then-insert."""
    slug = doctype.lower().replace(" ", "-")
    return f"transpire-{site_id}-{slug}-{event}"


@dataclasses.dataclass(frozen=True)
class DesiredWebhook:
    """One Webhook document as Transpire wants it on the site."""

    name: str
    doctype: str
    event: str
    request_url: str
    site_id: str
    secret: str = dataclasses.field(repr=False)

    def as_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "webhook_doctype": self.doctype,
            "webhook_docevent": self.event,
            "request_url": self.request_url,
            "request_method": "POST",
            "request_structure": "JSON",
            "webhook_json": DOORBELL_TEMPLATE,
            "webhook_headers": [
                {"key": "Content-Type", "value": "application/json"},
                {"key": SITE_HEADER, "value": self.site_id},
            ],
            "enable_security": 1,
            "webhook_secret": self.secret,
            "enabled": 1,
            "timeout": TIMEOUT_SECONDS,
        }


def purchase_order_webhooks(*, site_id: str, request_url: str, secret: str) -> tuple[DesiredWebhook, ...]:
    """The three Purchase Order Webhooks the connector listens to."""
    return tuple(
        DesiredWebhook(
            name=webhook_name(site_id, PURCHASE_ORDER, event),
            doctype=PURCHASE_ORDER,
            event=event,
            request_url=request_url,
            site_id=site_id,
            secret=secret,
        )
        for event in PURCHASE_ORDER_EVENTS
    )


@dataclasses.dataclass(frozen=True)
class WebhookReport:
    """What `ensure_webhooks` did: the names it created and the names it re-asserted."""

    created: list[str]
    updated: list[str]


def ensure_webhooks(client: _Writer, webhooks: Iterable[DesiredWebhook]) -> WebhookReport:
    """Create every Webhook the site lacks; re-assert the machine-owned fields on the rest.

    A Transpire Webhook is ours by name, so an existing one is brought back to
    the desired document -- URL, headers, template, secret, enabled, timeout --
    rather than left alone. That is what lets a re-run apply a changed gateway
    URL or a re-derived secret after the master secret rotates; the secret is
    a Password field Frappe never reads back, so it is always written.

    Returns:
        The names created and the names updated, each in input order.
    """
    created: list[str] = []
    updated: list[str] = []
    for hook in webhooks:
        document = hook.as_document()
        if ensure_by_name(client, DOCTYPE_WEBHOOK, hook.name, document):
            created.append(hook.name)
            continue
        client.update(DOCTYPE_WEBHOOK, hook.name, {k: v for k, v in document.items() if k != "name"})
        updated.append(hook.name)
    return WebhookReport(created=created, updated=updated)
