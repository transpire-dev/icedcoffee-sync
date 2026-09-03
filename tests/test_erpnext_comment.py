"""Comments on a document, through Frappe's own whitelisted method."""

from typing import Any

from stolon.erpnext.comment import add_comment, list_comments


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.listed: list[tuple[str, dict[str, Any]]] = []

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((dotted_path, params))
        return {"name": "cmt-1", "content": params["content"] if params else None}

    def list(self, doctype: str, **kwargs: Any) -> Any:
        self.listed.append((doctype, kwargs))
        return [{"name": "cmt-1", "content": "hello"}]


def test_add_comment_goes_through_add_comment_not_a_raw_insert() -> None:
    site = Recorder()
    out = add_comment(site, "Purchase Order", "PUR-ORD-1", "Tracking 1Z999 via UPS")
    assert out["name"] == "cmt-1"
    path, params = site.calls[0]
    assert path == "frappe.desk.form.utils.add_comment"
    assert params == {
        "reference_doctype": "Purchase Order",
        "reference_name": "PUR-ORD-1",
        "content": "Tracking 1Z999 via UPS",
        "comment_email": "integration@example.com",
        "comment_by": "Transpire",
    }


def test_list_comments_filters_to_the_document() -> None:
    site = Recorder()
    assert list_comments(site, "Purchase Order", "PUR-ORD-1") == [{"name": "cmt-1", "content": "hello"}]
    doctype, kwargs = site.listed[0]
    assert doctype == "Comment"
    assert kwargs["filters"] == {
        "reference_doctype": "Purchase Order",
        "reference_name": "PUR-ORD-1",
        "comment_type": "Comment",
    }
