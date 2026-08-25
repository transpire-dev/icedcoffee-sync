"""The draft-submit-cancel lifecycle of submittable doctypes.

A plain insert creates a draft at docstatus 0. Submitting is a separate call,
which makes "machines draft, humans submit" enforceable rather than merely
agreed.
"""

from enum import IntEnum


class DocStatus(IntEnum):
    """Frappe's document states."""

    DRAFT = 0
    SUBMITTED = 1
    CANCELLED = 2


class SubmitNotPermittedError(RuntimeError):
    """Raised when a client without submit capability touches the submit path.

    Covers `submit`/`cancel` directly, a `call` naming a known committing
    method, and an `insert`/`update` whose document body sets `docstatus`.

    Not a server error -- the call is refused locally, before anything is
    sent. This is a guardrail against an accidental submission from client
    code, not a security boundary: the real boundary is the API key's role
    permissions on the server, since a Frappe app can whitelist its own
    committing methods that this client has no way to know about.
    """
