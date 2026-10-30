import json

import pytest

from icedcoffee_sync.frappe.request import (
    auth_header,
    call_method,
    delete_document,
    get_document,
    insert_document,
    list_documents,
    update_document,
    upload_file,
)


def test_auth_header_uses_frappe_token_scheme() -> None:
    assert auth_header("KEY", "SECRET") == {"Authorization": "token KEY:SECRET"}


def test_list_targets_the_v2_document_surface() -> None:
    request = list_documents("Account")
    assert request.method == "GET"
    assert request.path == "/api/v2/document/Account"


def test_list_encodes_fields_and_filters_as_json() -> None:
    request = list_documents(
        "Account",
        fields=["name", "account_number"],
        filters=[["is_group", "=", 0]],
    )
    assert json.loads(request.params["fields"]) == ["name", "account_number"]
    assert json.loads(request.params["filters"]) == [["is_group", "=", 0]]


def test_list_omits_absent_options_entirely() -> None:
    request = list_documents("Account")
    assert request.params == {}


def test_list_passes_pagination_and_ordering() -> None:
    # v2 spells the offset `start`. `offset` is silently ignored -- it does not
    request = list_documents("Account", limit=50, start=100, order_by="name asc")
    assert request.params["limit"] == "50"
    assert request.params["start"] == "100"
    assert request.params["order_by"] == "name asc"
    assert "offset" not in request.params


def test_doctype_and_name_are_url_quoted() -> None:
    request = get_document("Journal Entry", "ACC-JV-2026-00001")
    assert request.path == "/api/v2/document/Journal%20Entry/ACC-JV-2026-00001"


def test_insert_posts_the_document_body() -> None:
    request = insert_document("Account", {"account_name": "Cash"})
    assert request.method == "POST"
    assert request.path == "/api/v2/document/Account"
    assert request.json_body == {"account_name": "Cash"}


def test_update_puts_to_the_named_document() -> None:
    request = update_document("Account", "Cash - T", {"account_number": "1000"})
    assert request.method == "PUT"
    assert request.path == "/api/v2/document/Account/Cash%20-%20T"
    assert request.json_body == {"account_number": "1000"}


def test_delete_targets_the_named_document() -> None:
    request = delete_document("Account", "Cash - T")
    assert request.method == "DELETE"
    assert request.path == "/api/v2/document/Account/Cash%20-%20T"


def test_method_calls_use_the_v2_method_surface() -> None:
    request = call_method("frappe.client.submit", {"doc": "{}"})
    assert request.method == "POST"
    assert request.path == "/api/v2/method/frappe.client.submit"
    assert request.json_body == {"doc": "{}"}


def test_names_containing_slashes_are_escaped() -> None:
    # ERPNext naming series can contain forward slashes; unescaped they would
    request = get_document("Sales Order", "SO/2024/00001")
    assert request.path == "/api/v2/document/Sales%20Order/SO%2F2024%2F00001"


def test_requests_are_frozen() -> None:
    import dataclasses

    import pytest

    request = list_documents("Account")
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.method = "POST"  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "hostile",
    [
        # HTTP drops a fragment, so the server would receive the bare method
        "frappe.client.submit#suffix",
        "frappe.client.submit?x=1",
        "frappe.client.submit/extra",
        "frappe.client.submit ",
        "",
    ],
)
def test_call_method_rejects_anything_but_a_dotted_path(hostile: str) -> None:
    with pytest.raises(ValueError, match="dotted method path"):
        call_method(hostile)


def test_call_method_accepts_a_normal_dotted_path() -> None:
    assert call_method("frappe.client.get_count").path == "/api/v2/method/frappe.client.get_count"


def test_get_document_rejects_an_empty_name() -> None:
    # An empty name is falsy; treating it as absent would silently turn a
    with pytest.raises(ValueError, match="must not be empty"):
        get_document("Account", "")


def test_delete_document_rejects_an_empty_name() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        delete_document("Account", "")


def test_update_document_rejects_an_empty_name() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        update_document("Account", "", {"account_name": "Cash"})


def test_call_method_rejects_a_trailing_newline() -> None:
    # `$` matches before a trailing newline, so `match()` would accept this
    with pytest.raises(ValueError, match="dotted method path"):
        call_method("frappe.client.submit\n")


def test_upload_file_targets_the_v2_upload_endpoint() -> None:
    request = upload_file("Purchase Invoice", "PINV-0001", "invoice.pdf", b"%PDF-1.4")
    assert request.method == "POST"
    assert request.path == "/api/v2/method/upload_file"


def test_upload_file_sends_the_content_as_a_multipart_file_part() -> None:
    request = upload_file("Purchase Invoice", "PINV-0001", "invoice.pdf", b"%PDF-1.4")
    assert request.files == {"file": ("invoice.pdf", b"%PDF-1.4")}
    assert request.json_body is None


def test_upload_file_sends_doctype_docname_as_form_fields() -> None:
    request = upload_file("Purchase Invoice", "PINV-0001", "invoice.pdf", b"content")
    assert request.data is not None
    assert request.data["doctype"] == "Purchase Invoice"
    assert request.data["docname"] == "PINV-0001"


def test_upload_file_defaults_to_private() -> None:
    request = upload_file("Purchase Invoice", "PINV-0001", "invoice.pdf", b"content")
    assert request.data is not None
    assert request.data["is_private"] == 1


def test_upload_file_can_be_made_public() -> None:
    request = upload_file("Purchase Invoice", "PINV-0001", "invoice.pdf", b"content", is_private=False)
    assert request.data is not None
    assert request.data["is_private"] == 0


def test_upload_file_omits_folder_when_not_given() -> None:
    request = upload_file("Purchase Invoice", "PINV-0001", "invoice.pdf", b"content")
    assert request.data is not None
    assert "folder" not in request.data


def test_upload_file_sends_folder_when_given() -> None:
    request = upload_file("Purchase Invoice", "PINV-0001", "invoice.pdf", b"content", folder="Home/Attachments")
    assert request.data is not None
    assert request.data["folder"] == "Home/Attachments"
