import json

import httpx2
import pytest
from stolon.frappe.client import AsyncClient, Client, SiteConfig, _send_args
from stolon.frappe.errors import DoesNotExistError
from stolon.frappe.request import Request

CONFIG = SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET")  # noqa: S106


def _transport(handler):
    return httpx2.MockTransport(handler)


def test_sync_client_sends_the_auth_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(request.headers)
        return httpx2.Response(200, json={"data": []})

    with Client(CONFIG, transport=_transport(handler)) as client:
        client.list("Account")

    assert seen["authorization"] == "token KEY:SECRET"


def test_sync_client_returns_unwrapped_rows() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"name": "Cash"}]})

    with Client(CONFIG, transport=_transport(handler)) as client:
        assert client.list("Account") == [{"name": "Cash"}]


def test_sync_client_raises_typed_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"exc_type": "DoesNotExistError"})

    with Client(CONFIG, transport=_transport(handler)) as client, pytest.raises(DoesNotExistError):
        client.get("Account", "Nope")


def test_sync_client_builds_the_expected_url() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(200, json={"data": {}})

    with Client(CONFIG, transport=_transport(handler)) as client:
        client.get("Account", "Cash - T")

    assert seen == ["https://example.test/api/v2/document/Account/Cash%20-%20T"]


async def test_async_client_returns_the_same_payload_as_sync() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": [{"name": "Cash"}]})

    async with AsyncClient(CONFIG, transport=_transport(handler)) as client:
        assert await client.list("Account") == [{"name": "Cash"}]


async def test_async_client_raises_the_same_typed_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"exc_type": "DoesNotExistError"})

    async with AsyncClient(CONFIG, transport=_transport(handler)) as client:
        with pytest.raises(DoesNotExistError):
            await client.get("Account", "Nope")


def test_insert_sends_the_document_as_json() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"data": {"name": "Cash - T"}})

    with Client(CONFIG, transport=_transport(handler)) as client:
        client.insert("Account", {"account_name": "Cash"})

    assert seen["body"] == {"account_name": "Cash"}


def test_caller_headers_cannot_replace_the_credential() -> None:
    # _send_args must apply the auth header last, so a Request carrying its
    # own Authorization key cannot silently displace the real credential.
    hostile = Request("GET", "/api/v2/document/Account", headers={"Authorization": "token evil:evil"})
    sent = _send_args(CONFIG, hostile)
    assert sent["headers"]["Authorization"] == "token KEY:SECRET"


def test_both_clients_expose_the_same_method_names() -> None:
    public = {n for n in dir(Client) if not n.startswith("_")}
    assert public == {n for n in dir(AsyncClient) if not n.startswith("_")}


def test_async_client_methods_are_actually_async() -> None:
    # `dir()` cannot tell `def` from `async def`, so a method accidentally
    # left synchronous on AsyncClient would pass a name-only comparison.
    import inspect

    for name in (n for n in dir(AsyncClient) if not n.startswith("_")):
        attribute = getattr(AsyncClient, name)
        if not callable(attribute):
            continue
        assert inspect.iscoroutinefunction(attribute) or inspect.isasyncgenfunction(attribute), (
            f"AsyncClient.{name} is not awaitable"
        )


def test_both_clients_take_the_same_parameters() -> None:
    # A parameter added to one side only is the other way these drift.
    import inspect

    for name in (n for n in dir(Client) if not n.startswith("_")):
        sync_attribute = getattr(Client, name)
        async_attribute = getattr(AsyncClient, name)
        if not (callable(sync_attribute) and callable(async_attribute)):
            continue
        # Compare Parameter objects, not just names: that also catches a
        # default value (or kind, or annotation) that drifts between the two
        # while the parameter list itself stays aligned.
        sync_params = list(inspect.signature(sync_attribute).parameters.values())
        async_params = list(inspect.signature(async_attribute).parameters.values())
        assert sync_params == async_params, f"{name} signatures differ"


def test_site_config_repr_hides_the_credentials() -> None:
    # A dataclass prints every field by default, and this object reaches log
    # lines, tracebacks, and failing pytest assertions -- any of which would
    # publish a reusable Frappe credential.
    rendered = repr(SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET"))  # noqa: S106
    assert "KEY" not in rendered
    assert "SECRET" not in rendered
    assert "example.test" in rendered


def test_lowercase_authorization_header_cannot_sit_alongside_the_credential() -> None:
    # HTTP header names are case-insensitive, so a lowercase `authorization`
    # would otherwise survive the merge next to the real one -- two credentials
    # on one request, with the server free to pick either.
    from stolon.frappe.client import _merge_headers

    hostile = Request("GET", "/api/v2/document/Account", headers={"authorization": "token evil:evil"})
    headers = _merge_headers(CONFIG, hostile)
    assert [k for k in headers if k.lower() == "authorization"] == ["Authorization"]
    assert headers["Authorization"] == "token KEY:SECRET"
