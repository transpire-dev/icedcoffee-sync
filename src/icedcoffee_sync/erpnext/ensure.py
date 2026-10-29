"""Get-or-insert by document name.

Frappe addresses a document by its name, and for the doctypes we provision
(Custom Field, Webhook) the name is deterministic -- either Frappe derives it
from the content or the caller sets it. So "make sure this exists" is one
`get` that is allowed to miss, then one `insert`. An existing document is
left exactly as it is: re-running setup must never rewrite what a person
changed on the site.
"""

from __future__ import annotations

from typing import Any, Protocol

from icedcoffee_sync.frappe.errors import DoesNotExistError


class _Writer(Protocol):
    def get(self, doctype: str, name: str) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...


def ensure_by_name(client: _Writer, doctype: str, name: str, doc: dict[str, Any]) -> bool:
    """Insert `doc` as `doctype` unless a document named `name` already exists.

    Returns:
        True when this call inserted it; False when it was already there.
    """
    try:
        client.get(doctype, name)
    except DoesNotExistError:
        client.insert(doctype, doc)
        return True
    return False
