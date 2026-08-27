"""Live tests against a real Frappe site.

Skipped unless STOLON_TEST_* is set. Points at the integration instance, never
at the books site -- the fixture asserts that rather than trusting the operator.
"""

import contextlib
import os
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from stolon.frappe.client import Client, SiteConfig
from stolon.frappe.errors import DoesNotExistError
from stolon.frappe.lifecycle import DocStatus, SubmitNotPermittedError

pytestmark = pytest.mark.live_frappe

# Every host these tests may run against -- an ALLOWLIST, not a denylist:
_TEST_SITE_HOSTS = frozenset({"transpire-test.frappe.cloud"})


def _is_test_site(url: str) -> bool:
    """True when `url`'s host, once normalised, is in `_TEST_SITE_HOSTS`.

    Host compared exactly, not via substring on the whole URL: a raw
    substring check would miss an allowlisted host appearing as a path or
    query segment on an unrelated URL, or refuse a legitimate URL that
    merely mentions it somewhere other than its host. The trailing-dot
    strip matters because a fully-qualified host
    ("transpire-test.frappe.cloud.") resolves to the identical site.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host in _TEST_SITE_HOSTS


@pytest.fixture
def client() -> Iterator[Client]:
    url = os.environ.get("STOLON_TEST_URL")
    key = os.environ.get("STOLON_TEST_KEY")
    secret = os.environ.get("STOLON_TEST_SECRET")
    if not (url and key and secret):
        pytest.skip("STOLON_TEST_URL/KEY/SECRET not set")
    if not _is_test_site(url):
        # Fail, never skip. A skip here would read as "nothing to run" while
        pytest.fail(f"refusing to run tests against a host not in _TEST_SITE_HOSTS: {url}")
    with Client(SiteConfig(url=url, api_key=key, api_secret=secret)) as c:
        yield c


def test_lists_accounts(client: Client) -> None:
    rows = client.list("Account", fields=["name"], limit=5)
    assert isinstance(rows, list)
    assert all("name" in row for row in rows)


def test_gets_a_single_account(client: Client) -> None:
    first = client.list("Account", fields=["name"], limit=1)
    if not first:
        pytest.skip("site has no accounts")
    doc = client.get("Account", first[0]["name"])
    assert doc["name"] == first[0]["name"]


def test_missing_document_raises_does_not_exist(client: Client) -> None:
    with pytest.raises(DoesNotExistError):
        client.get("Account", f"no-such-account-{uuid.uuid4()}")


def test_iter_all_returns_at_least_a_full_page(client: Client) -> None:
    # A small page_size forces multiple requests on any site with more
    page_size = 10

    # Establish the true count independently first. Deciding whether to skip
    reference_limit = 1000
    everything, reference_truncated = client._list_page("Account", reference_limit, 0, fields=["name"])
    if reference_truncated:
        # The reference is itself truncated, so it is not a total and cannot be
        pytest.skip(f"site has more than {reference_limit} accounts; reference list is truncated")
    if len(everything) <= page_size:
        pytest.skip(
            f"site has {len(everything)} accounts; too few to exercise multi-page paging at page_size={page_size}"
        )

    rows = list(client.iter_all("Account", fields=["name"], page_size=page_size))
    names = [row["name"] for row in rows]
    assert len(names) == len(set(names)), "paging returned duplicates"
    assert len(names) == len(everything), (
        f"iter_all returned {len(names)} of {len(everything)} accounts -- paging truncated"
    )


def test_v2_returns_empty_fields_so_null_is_distinguishable(client: Client) -> None:
    # The whole reason for targeting v2. If this fails, revisit response.field().
    first = client.list("Account", fields=["name"], limit=1)
    if not first:
        pytest.skip("site has no accounts")
    doc = client.get("Account", first[0]["name"])
    assert "account_number" in doc, "v2 should include empty fields"


def test_submit_is_refused_without_the_capability(client: Client) -> None:
    with pytest.raises(SubmitNotPermittedError):
        client.submit({"doctype": "Journal Entry", "name": "does-not-matter"})


def test_docstatus_enum_matches_a_real_document(client: Client) -> None:
    rows = client.list("Account", fields=["name"], limit=1)
    if not rows:
        pytest.skip("site has no accounts")
    doc = client.get("Account", rows[0]["name"])
    # Absence isn't proof of correctness -- it just means this particular
    if "docstatus" in doc:
        assert doc["docstatus"] in {int(s) for s in DocStatus}


@pytest.fixture
def note(client: Client) -> Iterator[tuple[Client, str, str]]:
    """A freshly created Note, deleted afterward regardless of test outcome.

    `Note` over `Account`: harmless and non-submittable, so a failed teardown
    never leaves junk in a chart of accounts a later live test diffs against.
    The title carries a uuid4 so concurrent runs can never collide on it, even
    though Frappe's own `name` for a Note is a server-assigned hash rather
    than something this client controls.
    """
    title = f"stolon-live-test-{uuid.uuid4()}"
    doc = client.insert("Note", {"title": title, "content": "<p>before</p>"})
    name = doc["name"]
    try:
        yield client, name, title
    finally:
        # Already gone is fine -- the test itself deletes it as the last step
        with contextlib.suppress(DoesNotExistError):
            client.delete("Note", name)


def test_write_round_trip_create_fetch_update_fetch_delete(note: tuple[Client, str, str]) -> None:
    client, name, title = note

    fetched = client.get("Note", name)
    assert fetched["title"] == title
    assert fetched["content"] == "<p>before</p>"

    updated = client.update("Note", name, {"content": "<p>after</p>"})
    assert updated["content"] == "<p>after</p>"

    fetched_after_update = client.get("Note", name)
    assert fetched_after_update["content"] == "<p>after</p>"

    client.delete("Note", name)
    with pytest.raises(DoesNotExistError):
        client.get("Note", name)


def test_call_round_trips_a_safe_read_only_rpc(client: Client) -> None:
    # frappe.client.get_count is whitelisted, read-only, and returns a bare
    count = client.call("frappe.client.get_count", {"doctype": "Account"})
    assert isinstance(count, int)
    assert count >= 0


def _minimal_pdf_bytes() -> bytes:
    """A structurally valid, byte-accurate minimal single-page PDF.

    Frappe scans every uploaded PDF for embedded JavaScript on save
    (`pdf_contains_js` in `frappe/utils/pdf.py`, via `pypdf.PdfReader`), so an
    arbitrary byte string with a `%PDF` header is not enough -- it must
    actually parse. A hand-rolled placeholder without a real cross-reference
    table failed live with `PdfStreamError: Stream has ended unexpectedly`
    before this was built with correct byte offsets.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, content in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode()
        body += content + b"\n"
        body += b"endobj\n"
    xref_offset = len(body)
    n = len(objects) + 1
    xref = bytearray(f"xref\n0 {n}\n".encode())
    xref += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        xref += f"{off:010d} 00000 n \n".encode()
    body += xref
    body += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    return bytes(body)


def test_checked_in_account_types_match_the_site(client: Client) -> None:
    # The offline snapshot is what lets a chart be validated without a network
    from stolon.erpnext.accounts import ACCOUNT_TYPES, live_account_types

    live = live_account_types(client)
    assert live == ACCOUNT_TYPES, {
        "only_on_site": sorted(live - ACCOUNT_TYPES),
        "only_checked_in": sorted(ACCOUNT_TYPES - live),
    }


def test_upload_file_attaches_a_private_file_to_a_document(client: Client) -> None:
    # Note over Account for the same reason the `note` fixture picks it:
    title = f"stolon-live-test-{uuid.uuid4()}"
    note = client.insert("Note", {"title": title, "content": "<p>upload target</p>"})
    note_name = note["name"]
    file_name: str | None = None
    try:
        content = _minimal_pdf_bytes()
        uploaded = client.upload_file("Note", note_name, "evidence.pdf", content)
        file_name = uploaded["name"]

        assert uploaded["file_name"] == "evidence.pdf"
        assert uploaded["attached_to_doctype"] == "Note"
        assert uploaded["attached_to_name"] == note_name
        assert bool(uploaded["is_private"]), "upload_file must default to private"

        fetched = client.get("File", file_name)
        assert fetched["attached_to_doctype"] == "Note"
        assert fetched["attached_to_name"] == note_name
        assert bool(fetched["is_private"])
    finally:
        if file_name is not None:
            with contextlib.suppress(DoesNotExistError):
                client.delete("File", file_name)
        with contextlib.suppress(DoesNotExistError):
            client.delete("Note", note_name)
