import json
from typing import Any

import httpx2
import pytest

from icedcoffee_sync.frappe.client import AsyncClient, Client, SiteConfig
from icedcoffee_sync.frappe.lifecycle import DocStatus, SubmitNotPermittedError

DRAFTS_ONLY = SiteConfig(url="https://example.test", api_key="K", api_secret="S")  # noqa: S106
MAY_SUBMIT = SiteConfig(
    url="https://example.test",
    api_key="K",
    api_secret="S",  # noqa: S106
    may_submit=True,
)


def _ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json={"data": {"docstatus": 1}})


def test_docstatus_values_match_frappe() -> None:
    assert (DocStatus.DRAFT, DocStatus.SUBMITTED, DocStatus.CANCELLED) == (0, 1, 2)


def test_submit_is_refused_by_default() -> None:
    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(_ok)) as client, pytest.raises(SubmitNotPermittedError):
        client.submit({"doctype": "Journal Entry", "name": "ACC-JV-0001"})


def test_cancel_is_refused_by_default() -> None:
    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(_ok)) as client, pytest.raises(SubmitNotPermittedError):
        client.cancel("Journal Entry", "ACC-JV-0001")


def test_refusal_happens_before_any_request_is_sent() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client, pytest.raises(SubmitNotPermittedError):
        client.submit({"doctype": "Journal Entry", "name": "ACC-JV-0001"})

    assert sent == []


def test_submit_calls_frappe_client_submit_when_permitted() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json={"data": {"docstatus": 1}})

    with Client(MAY_SUBMIT, transport=httpx2.MockTransport(handler)) as client:
        client.submit({"doctype": "Journal Entry", "name": "ACC-JV-0001"})

    assert seen == ["https://example.test/api/v2/method/frappe.client.submit"]


def test_submit_sends_the_whole_document_not_a_doctype_and_name() -> None:
    bodies: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"data": {"docstatus": 1}})

    doc = {"doctype": "Journal Entry", "name": "ACC-JV-0001"}
    with Client(MAY_SUBMIT, transport=httpx2.MockTransport(handler)) as client:
        client.submit(doc)

    assert bodies == [{"doc": doc}]


async def test_async_submit_is_refused_by_default() -> None:
    async with AsyncClient(DRAFTS_ONLY, transport=httpx2.MockTransport(_ok)) as client:
        with pytest.raises(SubmitNotPermittedError):
            await client.submit({"doctype": "Journal Entry", "name": "ACC-JV-0001"})


async def test_async_submit_sends_the_whole_document_not_a_doctype_and_name() -> None:
    bodies: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"data": {"docstatus": 1}})

    doc = {"doctype": "Journal Entry", "name": "ACC-JV-0001"}
    async with AsyncClient(MAY_SUBMIT, transport=httpx2.MockTransport(handler)) as client:
        await client.submit(doc)

    assert bodies == [{"doc": doc}]


def test_call_cannot_smuggle_a_submit_past_the_guard() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client, pytest.raises(SubmitNotPermittedError):
        client.call("frappe.client.submit", {"doctype": "Journal Entry", "name": "ACC-JV-0001"})

    assert sent == []


def test_call_still_allows_methods_that_do_not_commit() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": {"ok": True}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client:
        assert client.call("frappe.client.get_count", {"doctype": "Account"}) == {"ok": True}


def test_call_allows_a_committing_method_when_permitted() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json={"data": {"docstatus": 1}})

    with Client(MAY_SUBMIT, transport=httpx2.MockTransport(handler)) as client:
        client.call("frappe.client.submit", {"doctype": "Journal Entry", "name": "ACC-JV-0001"})

    assert seen == ["https://example.test/api/v2/method/frappe.client.submit"]


async def test_async_call_cannot_smuggle_a_submit_past_the_guard() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    async with AsyncClient(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(SubmitNotPermittedError):
            await client.call("frappe.client.submit", {"doctype": "Journal Entry", "name": "ACC-JV-0001"})

    assert sent == []


def test_insert_with_docstatus_is_refused_by_default() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client, pytest.raises(SubmitNotPermittedError):
        client.insert("Journal Entry", {"voucher_type": "Journal Entry", "docstatus": 1})

    assert sent == []


def test_update_with_docstatus_is_refused_by_default() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client, pytest.raises(SubmitNotPermittedError):
        client.update("Journal Entry", "ACC-JV-0001", {"docstatus": 1})

    assert sent == []


def test_insert_with_docstatus_zero_is_also_refused() -> None:
    # The rule is "don't touch this field", not "don't set it to something
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client, pytest.raises(SubmitNotPermittedError):
        client.insert("Journal Entry", {"voucher_type": "Journal Entry", "docstatus": 0})

    assert sent == []


def test_insert_without_docstatus_still_works_on_a_drafts_only_client() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": {"name": "ACC-JV-0001"}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client:
        assert client.insert("Journal Entry", {"voucher_type": "Journal Entry"}) == {"name": "ACC-JV-0001"}


def test_insert_with_docstatus_is_allowed_when_permitted() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json={"data": {"name": "ACC-JV-0001", "docstatus": 1}})

    with Client(MAY_SUBMIT, transport=httpx2.MockTransport(handler)) as client:
        client.insert("Journal Entry", {"voucher_type": "Journal Entry", "docstatus": 1})

    assert seen == ["https://example.test/api/v2/document/Journal%20Entry"]


def test_call_apply_workflow_is_refused_by_default() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    with Client(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client, pytest.raises(SubmitNotPermittedError):
        client.call(
            "frappe.model.workflow.apply_workflow",
            {"doctype": "Journal Entry", "name": "ACC-JV-0001", "action": "Submit"},
        )

    assert sent == []


async def test_async_insert_with_docstatus_is_refused_by_default() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    async with AsyncClient(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(SubmitNotPermittedError):
            await client.insert("Journal Entry", {"voucher_type": "Journal Entry", "docstatus": 1})

    assert sent == []


async def test_async_update_with_docstatus_is_refused_by_default() -> None:
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    async with AsyncClient(DRAFTS_ONLY, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(SubmitNotPermittedError):
            await client.update("Journal Entry", "ACC-JV-0001", {"docstatus": 1})

    assert sent == []
