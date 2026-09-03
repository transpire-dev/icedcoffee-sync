"""Comments on a Frappe document.

Frappe exposes `frappe.desk.form.utils.add_comment(reference_doctype,
reference_name, content, comment_email, comment_by)` (verified on
version-16-hotfix), which checks read permission on the document and creates a
`Comment` of type "Comment". It is not a committing method, so a client
without submit capability may call it. Listing goes through the `Comment`
doctype directly, filtered to the document, which is how idempotency-by-content
is checked before adding one.
"""

from __future__ import annotations

from typing import Any, Protocol

ADD_COMMENT = "frappe.desk.form.utils.add_comment"
DOCTYPE_COMMENT = "Comment"
DEFAULT_EMAIL = "integration@example.com"
DEFAULT_BY = "Transpire"


class _Caller(Protocol):
    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any: ...
    def list(self, doctype: str, **kwargs: Any) -> Any: ...


def add_comment(
    client: _Caller,
    doctype: str,
    name: str,
    content: str,
    *,
    email: str = DEFAULT_EMAIL,
    by: str = DEFAULT_BY,
) -> dict[str, Any]:
    """Add a Comment to `doctype`/`name`. Returns the created Comment document."""
    return client.call(
        ADD_COMMENT,
        {
            "reference_doctype": doctype,
            "reference_name": name,
            "content": content,
            "comment_email": email,
            "comment_by": by,
        },
    )


def list_comments(client: _Caller, doctype: str, name: str) -> list[dict[str, Any]]:
    """Every "Comment"-type Comment on the document, with its content."""
    return client.list(
        DOCTYPE_COMMENT,
        fields=["name", "content", "comment_email", "creation"],
        filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Comment"},
        limit=1000,
    )
