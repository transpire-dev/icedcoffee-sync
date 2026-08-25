"""Typed exceptions for Frappe's error envelopes.

Frappe reports failures two different ways, and both are live. The v2 API sends
`{"errors": [{"type": ..., "message": ..., "exception": <traceback>}]}`. Older
apps and `/api/method/` endpoints send `exc_type` alongside `_server_messages`,
a JSON string holding a list of JSON strings -- two decodes, frequently absent
or malformed.

Everything here degrades to a usable exception rather than raising while
handling an error. A parser that throws while reporting a failure replaces a
diagnosable problem with a mysterious one.
"""

import json
from typing import Any


class FrappeError(Exception):
    """Base for every error returned by a Frappe site."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        status: int | None = None,
        messages: list[str] | None = None,
        server_traceback: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status = status
        self.messages = messages or []
        self.server_traceback = server_traceback


class ValidationError(FrappeError):
    """A document failed Frappe's validation."""


class PermissionError_(FrappeError):
    """The calling user lacks permission. Trailing underscore avoids the builtin."""


class DoesNotExistError(FrappeError):
    """The named document does not exist."""


class DuplicateEntryError(FrappeError):
    """A document with that name already exists."""


class LinkExistsError(FrappeError):
    """The document is linked from elsewhere and cannot be deleted."""


class TimestampMismatchError(FrappeError):
    """The document changed after it was read.

    Frappe detects conflicts by comparing the `modified` timestamp sent with a
    write against the stored one. This is optimistic concurrency failing, not a
    server fault -- callers should re-read and retry.
    """


class RateLimitError(FrappeError):
    """The site is rate limiting us."""


_BY_TYPE: dict[str, type[FrappeError]] = {
    "ValidationError": ValidationError,
    "MandatoryError": ValidationError,
    "PermissionError": PermissionError_,
    "DoesNotExistError": DoesNotExistError,
    "DuplicateEntryError": DuplicateEntryError,
    "LinkExistsError": LinkExistsError,
    "TimestampMismatch": TimestampMismatchError,
    "TimestampMismatchError": TimestampMismatchError,
}


def parse_errors(payload: Any) -> list[dict[str, Any]]:
    """Extract the v2 `errors` list, or an empty list if there isn't one."""
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    return [item for item in errors if isinstance(item, dict)]


def parse_server_messages(raw: str | None) -> list[str]:
    """Extract human-readable strings from Frappe's `_server_messages`.

    The value is a JSON string holding a list of JSON strings, each usually an
    object with a `message` key. Returns an empty list for anything it cannot
    decode -- a malformed diagnostic must never mask the error it describes.
    """
    if not raw:
        return []
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return []
    if not isinstance(outer, list):
        return []

    messages: list[str] = []
    for item in outer:
        if not isinstance(item, str):
            continue
        try:
            inner: Any = json.loads(item)
        except json.JSONDecodeError:
            messages.append(item)
            continue
        if isinstance(inner, dict) and "message" in inner:
            messages.append(str(inner["message"]))
        elif isinstance(inner, str):
            messages.append(inner)
    return messages


def error_from_response(status: int, body: str) -> FrappeError:
    """Build the most specific exception the response supports."""
    payload: dict[str, Any] = {}
    try:
        decoded = json.loads(body)
        if isinstance(decoded, dict):
            payload = decoded
    except json.JSONDecodeError:
        payload = {}

    error_type: str | None = None
    messages: list[str] = []
    server_traceback: str | None = None

    errors = parse_errors(payload)
    if errors:
        error_type = errors[0].get("type")
        messages = [str(item["message"]) for item in errors if item.get("message")]
        # Kept for debugging, deliberately never used as the message: it is a
        # server-side Python traceback, not something to show a person.
        raw_traceback = errors[0].get("exception")
        server_traceback = str(raw_traceback) if raw_traceback else None
    else:
        error_type = payload.get("exc_type")
        messages = parse_server_messages(payload.get("_server_messages"))

    message = messages[0] if messages else str(payload.get("message") or body[:500]).strip()

    if status == 429:
        cls: type[FrappeError] = RateLimitError
    else:
        cls = _BY_TYPE.get(str(error_type), FrappeError)

    return cls(
        message,
        error_type=error_type,
        status=status,
        messages=messages,
        server_traceback=server_traceback,
    )
