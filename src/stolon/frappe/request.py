"""Frappe requests as pure data.

Nothing here performs I/O. Both the sync and async clients build their calls
through these functions, so request shaping is tested once and cannot drift
between the two.
"""

import dataclasses
import json
import re
from typing import Any
from urllib.parse import quote

API_V2 = "/api/v2"


@dataclasses.dataclass(frozen=True)
class Request:
    """A fully described HTTP call, ready for either client to send."""

    method: str
    path: str
    params: dict[str, str] = dataclasses.field(default_factory=dict)
    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    json_body: dict[str, Any] | None = None


def auth_header(key: str, secret: str) -> dict[str, str]:
    """Frappe's API-key scheme: `Authorization: token <key>:<secret>`."""
    return {"Authorization": f"token {key}:{secret}"}


# A dotted path is a Python import path and nothing else. Validating it before
# it reaches a URL closes a bypass: `frappe.client.submit#x` is not a member of
# any denylist, but HTTP drops the fragment, so the server sees the bare method.
# The same holds for a `?query` suffix.
_DOTTED_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _document_path(doctype: str, name: str | None = None) -> str:
    path = f"{API_V2}/document/{quote(doctype, safe='')}"
    if name is None:
        return path
    if not name:
        # An empty name is falsy, and treating it as absent would silently turn
        # a single-document request into a list of the whole doctype.
        raise ValueError("document name must not be empty")
    return f"{path}/{quote(name, safe='')}"


def list_documents(
    doctype: str,
    *,
    fields: list[str] | None = None,
    filters: list[list[Any]] | dict[str, Any] | None = None,
    limit: int | None = None,
    start: int | None = None,
    order_by: str | None = None,
) -> Request:
    """List documents. Absent options are omitted rather than sent empty."""
    params: dict[str, str] = {}
    if fields is not None:
        params["fields"] = json.dumps(fields)
    if filters is not None:
        params["filters"] = json.dumps(filters)
    if limit is not None:
        params["limit"] = str(limit)
    if start is not None:
        params["start"] = str(start)
    if order_by is not None:
        params["order_by"] = order_by
    return Request("GET", _document_path(doctype), params)


def get_document(doctype: str, name: str) -> Request:
    """Fetch one document by name."""
    return Request("GET", _document_path(doctype, name))


def insert_document(doctype: str, doc: dict[str, Any]) -> Request:
    """Create a document. Submittable doctypes land as drafts at docstatus 0."""
    return Request("POST", _document_path(doctype), json_body=doc)


def update_document(doctype: str, name: str, doc: dict[str, Any]) -> Request:
    """Update a document in place."""
    return Request("PUT", _document_path(doctype, name), json_body=doc)


def delete_document(doctype: str, name: str) -> Request:
    """Delete a document."""
    return Request("DELETE", _document_path(doctype, name))


def call_method(dotted_path: str, params: dict[str, Any] | None = None) -> Request:
    """Call a whitelisted server method.

    Rejects anything that is not a plain dotted identifier path. A URL fragment
    or query suffix would survive a string comparison against a denylist while
    HTTP discarded it, so the server would receive a method the caller was
    supposed to be refused.
    """
    # fullmatch, not match: `$` also matches before a trailing newline, so
    # `frappe.client.submit\n` would pass while failing the exact denylist
    # comparison the guard makes.
    if not _DOTTED_PATH.fullmatch(dotted_path):
        raise ValueError(f"not a dotted method path: {dotted_path!r}")
    return Request("POST", f"{API_V2}/method/{dotted_path}", json_body=params or {})
