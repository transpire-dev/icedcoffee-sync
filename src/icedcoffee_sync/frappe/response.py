"""Response parsing, shared by both clients.

Frappe's v2 surface includes empty fields in its responses, which makes a
cleared field distinguishable from one that was never selected. That
distinction is the difference between "set this to null" and "leave it alone"
during a sync, so it is preserved rather than flattened onto None.
"""

import json
from typing import Any, Final

from icedcoffee_sync.frappe.errors import FrappeError, error_from_response


class _Missing:
    """Sentinel for a key that was not present at all."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Final[_Missing] = _Missing()


def envelope(status: int, body: str) -> Any:
    """Return the whole parsed payload, or raise a typed error.

    v2 list responses carry pagination alongside the rows --
    `{"has_next_page": true, "data": [...]}` -- so anything that pages needs
    the envelope rather than just its contents.
    """
    if status >= 400:
        raise error_from_response(status, body)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise FrappeError(f"expected JSON, got {body[:200]!r}", status=status) from exc


def unwrap(status: int, body: str) -> Any:
    """Return the response's `data` payload, or raise a typed error."""
    payload = envelope(status, body)
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def has_next_page(payload: Any) -> bool | None:
    """Whether the server says more pages exist.

    Returns None both when the payload carries no such flag -- v1 responses
    do not -- and when the flag is present but explicitly null. Either way
    the response cannot answer the question, and treating that as "no more
    pages" would stop a listing early.
    """
    if not isinstance(payload, dict) or "has_next_page" not in payload:
        return None
    value = payload["has_next_page"]
    if value is None:
        return None
    return bool(value)


def field(doc: dict[str, Any], name: str) -> Any:
    """Read a field, distinguishing absent (MISSING) from null (None)."""
    return doc.get(name, MISSING)
