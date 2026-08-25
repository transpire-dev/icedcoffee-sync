"""Synchronous and asynchronous Frappe clients.

Neither wraps the other. Request construction and response parsing are pure
functions shared by both, so only the send differs. A synchronous facade over
the async client would need `asyncio.run()`, which fails inside a marimo
notebook -- and notebooks are a first-class consumer.
"""

import dataclasses
from collections.abc import AsyncIterator, Iterator
from typing import Any, Self

import httpx2

from stolon.frappe import request as req
from stolon.frappe.lifecycle import SubmitNotPermittedError
from stolon.frappe.response import envelope, has_next_page, unwrap

# Both clients define a public `list` method, which shadows the builtin
# `list` type within their class bodies under Python 3.14's lazy annotation
# evaluation (PEP 649): any method annotated with a bare `list[...]` after
# that point resolves it to the method, not the type, and blows up the first
# time something evaluates the annotation. A module-level alias sidesteps it.
_Rows = list[dict[str, Any]]

# Whitelisted server methods that commit a document to the ledger (move it
# off docstatus 0). `call()` is a generic passthrough to any dotted server
# method, so without this it would be a way around `submit`/`cancel` entirely
# -- the guard has to sit on the method name, not just the friendly wrapper.
#
# This is a guardrail against an accidental submission from client code, not
# a sandbox: the list can't be exhaustive, since a Frappe app can whitelist
# its own committing methods this client has no way to know about. The real
# boundary is the API key's role permissions on the server.
_COMMITTING_METHODS = frozenset({
    "frappe.client.submit",
    "frappe.client.cancel",
    # Applies a workflow action, which calls doc.submit() when the action
    # moves a draft to a submitted state.
    "frappe.model.workflow.apply_workflow",
    # Dotted-path twins of insert/update, with the same docstatus mechanics
    # guarded by `_require_no_docstatus` below.
    "frappe.client.insert",
    "frappe.client.save",
})


@dataclasses.dataclass(frozen=True)
class SiteConfig:
    """Where a Frappe site is and how to authenticate to it.

    The URL is always supplied by the caller. Nothing here defaults to a
    production host.

    `may_submit` is off by default: a client must be deliberately constructed
    with the capability before `submit`/`cancel`, a `call` naming a known
    committing method, or an `insert`/`update` whose document body sets
    `docstatus`, will run. This is a guardrail against an accidental
    submission from client code, not a security boundary -- the real
    boundary is the API key's role permissions on the server.
    """

    url: str
    # Kept out of the generated repr: a dataclass prints every field, and this
    # object turns up in log lines, tracebacks, and failing pytest assertions.
    # A reusable Frappe credential should not ride along in any of them.
    api_key: str = dataclasses.field(repr=False)
    api_secret: str = dataclasses.field(repr=False)
    timeout: float = 30.0
    may_submit: bool = False


def _reject_reserved_pagination_kwargs(kwargs: dict[str, Any]) -> None:
    """Refuse query options `iter_all` supplies itself.

    `iter_all` owns `limit` and `start` -- it walks the pages. Forwarding a
    caller's copy alongside its own produced a duplicate-keyword `TypeError`
    from deep inside the pager, which tells the caller nothing about what they
    did wrong. `page_size` is the knob they wanted.
    """
    reserved = sorted(k for k in ("limit", "start") if k in kwargs)
    if reserved:
        raise TypeError(f"iter_all controls {', '.join(reserved)}; use page_size to size each request")


def _require_positive_page_size(page_size: int) -> None:
    """Refuse a page size that cannot page.

    A zero or negative size asks the server for nothing, and the empty result
    ends the loop on its first pass -- so `iter_all` returns silently having
    fetched no documents, which reads exactly like an empty doctype.
    """
    if page_size < 1:
        raise ValueError(f"page_size must be at least 1, got {page_size}")


def _merge_headers(config: SiteConfig, request: req.Request) -> dict[str, str]:
    """Caller headers, then the credential, which nothing may displace.

    Dropped case-insensitively: HTTP header names are case-insensitive, so a
    request carrying `authorization` would otherwise sit alongside the real
    `Authorization` rather than being overridden by it -- two credentials on
    one request, with the server free to pick either.
    """
    headers = {"Accept": "application/json"}
    headers.update({k: v for k, v in request.headers.items() if k.lower() != "authorization"})
    headers.update(req.auth_header(config.api_key, config.api_secret))
    return headers


def _send_args(config: SiteConfig, request: req.Request) -> dict[str, Any]:
    return {
        "method": request.method,
        "url": f"{config.url.rstrip('/')}{request.path}",
        "params": request.params or None,
        "headers": _merge_headers(config, request),
        "json": request.json_body,
    }


class Client:
    """Synchronous Frappe client."""

    def __init__(self, config: SiteConfig, *, transport: httpx2.BaseTransport | None = None) -> None:
        self._config = config
        self._http = httpx2.Client(timeout=config.timeout, transport=transport)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._http.close()

    def _run(self, request: req.Request) -> Any:
        response = self._http.request(**_send_args(self._config, request))
        return unwrap(response.status_code, response.text)

    def list(self, doctype: str, **kwargs: Any) -> Any:
        """List documents of a doctype."""
        return self._run(req.list_documents(doctype, **kwargs))

    def get(self, doctype: str, name: str) -> Any:
        """Fetch one document by name."""
        return self._run(req.get_document(doctype, name))

    def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
        """Create a document.

        Refuses locally, before anything is sent, if `doc` sets `docstatus`
        and this client lacks submit capability: Frappe's own insert path
        (`Document.check_docstatus_transition`) submits a document created
        with `docstatus: 1` directly, with no separate `submit()` call.
        """
        self._require_no_docstatus(doc)
        return self._run(req.insert_document(doctype, doc))

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        """Update a document.

        Refuses locally, before anything is sent, if `doc` sets `docstatus`
        and this client lacks submit capability -- same mechanism as
        `insert`.
        """
        self._require_no_docstatus(doc)
        return self._run(req.update_document(doctype, name, doc))

    def delete(self, doctype: str, name: str) -> Any:
        """Delete a document."""
        return self._run(req.delete_document(doctype, name))

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        """Call a whitelisted server method.

        Refuses locally, before anything is sent, when `dotted_path` names a
        known committing method (e.g. `frappe.client.submit`) and this client
        lacks submit capability -- `submit`/`cancel` are sugar over this, and
        the guard has to hold on this route too.
        """
        self._require_submit(dotted_path)
        return self._run(req.call_method(dotted_path, params))

    def _require_submit(self, dotted_path: str | None = None) -> None:
        if self._config.may_submit:
            return
        # Called with no path (from submit/cancel directly), this always
        # refuses. Called from `call()`, it refuses only for a method known
        # to commit -- everything else passes through unchecked.
        if dotted_path is not None and dotted_path not in _COMMITTING_METHODS:
            return
        raise SubmitNotPermittedError(
            "this client was constructed without submit capability; machines create drafts, humans submit"
        )

    def _require_no_docstatus(self, doc: dict[str, Any]) -> None:
        # A drafts-only client has no legitimate reason to set docstatus at
        # all -- a draft is 0, the default when the key is absent -- so this
        # refuses on the key's presence, not its value. `docstatus: 0` is
        # refused too: the rule is "don't touch this field", not "don't set
        # it to something dangerous".
        if not self._config.may_submit and "docstatus" in doc:
            raise SubmitNotPermittedError("this client cannot set docstatus; machines create drafts, humans submit")

    def submit(self, doc: dict[str, Any]) -> Any:
        """Submit a draft document, moving it to docstatus 1.

        Takes the whole document rather than a doctype and name, because
        Frappe's `frappe.client.submit` does -- unlike `cancel`, which takes
        the pair. The asymmetry is Frappe's, mirrored here rather than papered
        over.
        """
        self._require_submit()
        return self.call("frappe.client.submit", {"doc": doc})

    def cancel(self, doctype: str, name: str) -> Any:
        """Cancel a submitted document, moving it to docstatus 2."""
        self._require_submit()
        return self.call("frappe.client.cancel", {"doctype": doctype, "name": name})

    def _list_page(self, doctype: str, limit: int, start: int, **kwargs: Any) -> tuple[_Rows, bool | None]:
        """One page of documents, plus whether the server says more remain."""
        request = req.list_documents(doctype, limit=limit, start=start, **kwargs)
        response = self._http.request(**_send_args(self._config, request))
        payload = envelope(response.status_code, response.text)
        rows = payload["data"] if isinstance(payload, dict) and "data" in payload else payload
        return rows, has_next_page(payload)

    def iter_all(self, doctype: str, *, page_size: int = 100, **kwargs: Any) -> Iterator[dict[str, Any]]:
        """Yield every matching document, paging until the server says stop.

        Callers that page by hand eventually forget to, and a truncated read
        looks exactly like a small result set. v2 states whether more pages
        exist, so prefer that; fall back to the short-page heuristic only when
        the flag is absent, as on v1 responses.
        """
        _require_positive_page_size(page_size)
        _reject_reserved_pagination_kwargs(kwargs)
        start = 0
        while True:
            rows, more = self._list_page(doctype, page_size, start, **kwargs)
            if not rows:
                return
            yield from rows
            if more is False:
                return
            if more is None and len(rows) < page_size:
                return
            # Advance by what the server actually sent, not what was asked
            # for: a server that caps page size below `page_size` can return
            # fewer rows while still reporting has_next_page: true, and
            # advancing by page_size would skip the rows it never sent.
            start += len(rows)


class AsyncClient:
    """Asynchronous Frappe client. Same surface as `Client`, awaitable."""

    def __init__(self, config: SiteConfig, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._http = httpx2.AsyncClient(timeout=config.timeout, transport=transport)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the underlying connection pool."""
        await self._http.aclose()

    async def _run(self, request: req.Request) -> Any:
        response = await self._http.request(**_send_args(self._config, request))
        return unwrap(response.status_code, response.text)

    async def list(self, doctype: str, **kwargs: Any) -> Any:
        """List documents of a doctype."""
        return await self._run(req.list_documents(doctype, **kwargs))

    async def get(self, doctype: str, name: str) -> Any:
        """Fetch one document by name."""
        return await self._run(req.get_document(doctype, name))

    async def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
        """Create a document.

        Refuses locally, before anything is sent, if `doc` sets `docstatus`
        and this client lacks submit capability: Frappe's own insert path
        (`Document.check_docstatus_transition`) submits a document created
        with `docstatus: 1` directly, with no separate `submit()` call.
        """
        self._require_no_docstatus(doc)
        return await self._run(req.insert_document(doctype, doc))

    async def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        """Update a document.

        Refuses locally, before anything is sent, if `doc` sets `docstatus`
        and this client lacks submit capability -- same mechanism as
        `insert`.
        """
        self._require_no_docstatus(doc)
        return await self._run(req.update_document(doctype, name, doc))

    async def delete(self, doctype: str, name: str) -> Any:
        """Delete a document."""
        return await self._run(req.delete_document(doctype, name))

    async def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        """Call a whitelisted server method.

        Refuses locally, before anything is sent, when `dotted_path` names a
        known committing method (e.g. `frappe.client.submit`) and this client
        lacks submit capability -- `submit`/`cancel` are sugar over this, and
        the guard has to hold on this route too.
        """
        self._require_submit(dotted_path)
        return await self._run(req.call_method(dotted_path, params))

    def _require_submit(self, dotted_path: str | None = None) -> None:
        if self._config.may_submit:
            return
        # Called with no path (from submit/cancel directly), this always
        # refuses. Called from `call()`, it refuses only for a method known
        # to commit -- everything else passes through unchecked.
        if dotted_path is not None and dotted_path not in _COMMITTING_METHODS:
            return
        raise SubmitNotPermittedError(
            "this client was constructed without submit capability; machines create drafts, humans submit"
        )

    def _require_no_docstatus(self, doc: dict[str, Any]) -> None:
        # A drafts-only client has no legitimate reason to set docstatus at
        # all -- a draft is 0, the default when the key is absent -- so this
        # refuses on the key's presence, not its value. `docstatus: 0` is
        # refused too: the rule is "don't touch this field", not "don't set
        # it to something dangerous".
        if not self._config.may_submit and "docstatus" in doc:
            raise SubmitNotPermittedError("this client cannot set docstatus; machines create drafts, humans submit")

    async def submit(self, doc: dict[str, Any]) -> Any:
        """Submit a draft document, moving it to docstatus 1.

        Takes the whole document rather than a doctype and name, because
        Frappe's `frappe.client.submit` does -- unlike `cancel`, which takes
        the pair. The asymmetry is Frappe's, mirrored here rather than papered
        over.
        """
        self._require_submit()
        return await self.call("frappe.client.submit", {"doc": doc})

    async def cancel(self, doctype: str, name: str) -> Any:
        """Cancel a submitted document, moving it to docstatus 2."""
        self._require_submit()
        return await self.call("frappe.client.cancel", {"doctype": doctype, "name": name})

    async def _list_page(self, doctype: str, limit: int, start: int, **kwargs: Any) -> tuple[_Rows, bool | None]:
        """One page of documents, plus whether the server says more remain."""
        request = req.list_documents(doctype, limit=limit, start=start, **kwargs)
        response = await self._http.request(**_send_args(self._config, request))
        payload = envelope(response.status_code, response.text)
        rows = payload["data"] if isinstance(payload, dict) and "data" in payload else payload
        return rows, has_next_page(payload)

    async def iter_all(self, doctype: str, *, page_size: int = 100, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        """Yield every matching document, paging until the server says stop.

        Mirrors the sync twin exactly: prefer the server's `has_next_page`, and
        fall back to the short-page heuristic only when the flag is absent.
        """
        _require_positive_page_size(page_size)
        _reject_reserved_pagination_kwargs(kwargs)
        start = 0
        while True:
            rows, more = await self._list_page(doctype, page_size, start, **kwargs)
            if not rows:
                return
            for row in rows:
                yield row
            if more is False:
                return
            if more is None and len(rows) < page_size:
                return
            # Advance by what the server actually sent, not what was asked
            # for: a server that caps page size below `page_size` can return
            # fewer rows while still reporting has_next_page: true, and
            # advancing by page_size would skip the rows it never sent.
            start += len(rows)
