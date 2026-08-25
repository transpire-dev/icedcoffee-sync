import httpx2
import pytest
from stolon.frappe.client import AsyncClient, Client, SiteConfig

CONFIG = SiteConfig(url="https://example.test", api_key="K", api_secret="S")  # noqa: S106


def _paged_handler(total: int, page_size: int):
    def handler(request: httpx2.Request) -> httpx2.Response:
        start = int(request.url.params.get("start", 0))
        rows = [{"name": f"ACC-{i}"} for i in range(start, min(start + page_size, total))]
        return httpx2.Response(200, json={"data": rows})

    return handler


def test_iter_all_walks_every_page() -> None:
    with Client(CONFIG, transport=httpx2.MockTransport(_paged_handler(250, 100))) as client:
        names = [row["name"] for row in client.iter_all("Account", page_size=100)]
    assert len(names) == 250
    assert names[0] == "ACC-0"
    assert names[-1] == "ACC-249"


def test_iter_all_stops_on_a_short_page() -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        start = int(request.url.params.get("start", 0))
        rows = [{"name": f"ACC-{i}"} for i in range(start, min(start + 100, 150))]
        return httpx2.Response(200, json={"data": rows})

    with Client(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        list(client.iter_all("Account", page_size=100))

    # 100 rows, then 50 rows: the short page ends it, no wasted third request.
    assert len(calls) == 2


def test_iter_all_handles_an_empty_result() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": []})

    with Client(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        assert list(client.iter_all("Account")) == []


async def test_async_iter_all_walks_every_page() -> None:
    async with AsyncClient(CONFIG, transport=httpx2.MockTransport(_paged_handler(250, 100))) as client:
        names = [row["name"] async for row in client.iter_all("Account", page_size=100)]
    assert len(names) == 250


def test_iter_all_stops_when_the_server_says_so_even_on_a_full_page() -> None:
    # The short-page heuristic would keep going here: the page is exactly
    # full. has_next_page: false must override it.
    def handler(request: httpx2.Request) -> httpx2.Response:
        rows = [{"name": f"ACC-{i}"} for i in range(100)]
        return httpx2.Response(200, json={"has_next_page": False, "data": rows})

    with Client(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        rows = list(client.iter_all("Account", page_size=100))
    assert len(rows) == 100


def test_iter_all_keeps_paging_when_the_server_says_so_on_a_short_page() -> None:
    # The short-page heuristic would stop here: the page is short.
    # has_next_page: true must override it.
    pages = [
        {"has_next_page": True, "data": [{"name": "ACC-0"}]},
        {"has_next_page": False, "data": [{"name": "ACC-1"}]},
    ]
    seen: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(1)
        return httpx2.Response(200, json=pages[min(len(seen) - 1, 1)])

    with Client(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        rows = list(client.iter_all("Account", page_size=100))
    assert [r["name"] for r in rows] == ["ACC-0", "ACC-1"]


def test_iter_all_advances_by_rows_returned_not_page_size_on_a_capped_page() -> None:
    # A server that caps page size below what was asked for can return fewer
    # rows than page_size while still reporting has_next_page: true. Rows are
    # keyed by `start` here, so advancing by `page_size` (skipping ahead of
    # what the server actually sent) produces a detectable gap rather than
    # coincidentally passing.
    total = 25
    served_page_size = 10  # server caps below the requested page_size of 100

    def handler(request: httpx2.Request) -> httpx2.Response:
        start = int(request.url.params.get("start", 0))
        rows = [{"name": f"ACC-{i}"} for i in range(start, min(start + served_page_size, total))]
        return httpx2.Response(200, json={"has_next_page": start + len(rows) < total, "data": rows})

    with Client(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        names = [row["name"] for row in client.iter_all("Account", page_size=100)]

    assert names == [f"ACC-{i}" for i in range(total)]


async def test_async_iter_all_advances_by_rows_returned_not_page_size_on_a_capped_page() -> None:
    total = 25
    served_page_size = 10

    def handler(request: httpx2.Request) -> httpx2.Response:
        start = int(request.url.params.get("start", 0))
        rows = [{"name": f"ACC-{i}"} for i in range(start, min(start + served_page_size, total))]
        return httpx2.Response(200, json={"has_next_page": start + len(rows) < total, "data": rows})

    async with AsyncClient(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        names = [row["name"] async for row in client.iter_all("Account", page_size=100)]

    assert names == [f"ACC-{i}" for i in range(total)]


async def test_async_iter_all_stops_when_the_server_says_so_even_on_a_full_page() -> None:
    # Async twin of the sync case above: both clients implement has_next_page
    # handling independently, so both need the regression coverage.
    def handler(request: httpx2.Request) -> httpx2.Response:
        rows = [{"name": f"ACC-{i}"} for i in range(100)]
        return httpx2.Response(200, json={"has_next_page": False, "data": rows})

    async with AsyncClient(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        rows = [row async for row in client.iter_all("Account", page_size=100)]
    assert len(rows) == 100


@pytest.mark.parametrize("reserved", ["limit", "start"])
def test_iter_all_rejects_pagination_kwargs_it_controls(reserved: str) -> None:
    # Forwarding a caller's `limit`/`start` alongside the pager's own produced
    # a duplicate-keyword TypeError from inside the loop, which said nothing
    # useful. Refuse it up front with the knob they actually wanted.
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": []})

    with (
        Client(CONFIG, transport=httpx2.MockTransport(handler)) as client,
        pytest.raises(TypeError, match="use page_size"),
    ):
        list(client.iter_all("Account", **{reserved: 5}))


async def test_async_iter_all_rejects_pagination_kwargs_it_controls() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": []})

    async with AsyncClient(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(TypeError, match="use page_size"):
            [row async for row in client.iter_all("Account", limit=5)]


@pytest.mark.parametrize("bad", [0, -1])
def test_iter_all_rejects_a_page_size_that_cannot_page(bad: int) -> None:
    # A zero or negative size asks for nothing, the empty result ends the loop
    # on its first pass, and iter_all returns silently having fetched no
    # documents -- indistinguishable from an empty doctype.
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": []})

    with (
        Client(CONFIG, transport=httpx2.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="page_size must be at least 1"),
    ):
        list(client.iter_all("Account", page_size=bad))


async def test_async_iter_all_rejects_a_page_size_that_cannot_page() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": []})

    async with AsyncClient(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="page_size must be at least 1"):
            [row async for row in client.iter_all("Account", page_size=0)]
