"""Downloading a File's bytes: authenticated, capped, and only from the site itself."""

import httpx2
import pytest

from icedcoffee_sync.frappe.client import Client, FileDownloadError, SiteConfig

CONFIG = SiteConfig(url="https://site.example", api_key="k", api_secret="s", timeout=5.0)  # noqa: S106


def _serve(body: bytes, status: int = 200):
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, content=body)

    return handler, seen


def test_a_private_file_is_fetched_with_the_credential():
    handler, seen = _serve(b"%PDF-1.7 receipt")
    with Client(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        assert client.download_file("/private/files/r.pdf", max_bytes=1024) == b"%PDF-1.7 receipt"
    assert str(seen[0].url) == "https://site.example/private/files/r.pdf"
    assert seen[0].headers["Authorization"] == "token k:s"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/x.pdf",
        "//evil.example/x",
        "/api/method/frappe.client.submit",
        "files/x",
        "/private/files/../../../api/method/frappe.client.delete_doc",
        "/files/./../api/x",
        "/files/x?y=1",
        "/files/x#y",
        "/files/a\\b",
        "/private/files/%2e%2e/%2e%2e/api/x",
        # No leading "/files/" or "/private/files/" at all: concatenated
        # onto "https://site.example" this becomes
        # "https://site.example:8443/files/x" -- a PORT change, not a path,
        # that the old host-only check let through because the *parsed*
        # host and path both still looked fine.
        ":8443/files/x",
        # Parses as userinfo "site.example" on host "evil.example" --
        # refused by the host check too, but the raw-prefix check catches
        # it first.
        "@evil.example/files/x",
    ],
)
def test_anything_but_a_site_file_path_is_refused_before_sending(url):
    handler, seen = _serve(b"")
    with (
        Client(CONFIG, transport=httpx2.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="not a site file path"),
    ):
        client.download_file(url, max_bytes=10)
    assert not seen


def test_a_normal_file_path_still_succeeds_against_a_site_with_an_explicit_port():
    # The scheme/host/port equality check must not misfire on a legitimate
    # site whose own URL names a non-default port -- the file_url still
    # inherits that same port through plain concatenation.
    config = SiteConfig(url="https://site.example:9443", api_key="k", api_secret="s", timeout=5.0)  # noqa: S106
    handler, seen = _serve(b"ok")
    with Client(config, transport=httpx2.MockTransport(handler)) as client:
        assert client.download_file("/files/x", max_bytes=10) == b"ok"
    assert str(seen[0].url) == "https://site.example:9443/files/x"


def test_a_file_exactly_at_the_cap_succeeds():
    body = b"x" * 10
    handler, seen = _serve(body)
    with Client(CONFIG, transport=httpx2.MockTransport(handler)) as client:
        assert client.download_file("/files/exact.bin", max_bytes=10) == body
    assert len(seen) == 1


def test_a_file_over_the_cap_is_refused():
    handler, _ = _serve(b"x" * 11)
    with (
        Client(CONFIG, transport=httpx2.MockTransport(handler)) as client,
        pytest.raises(FileDownloadError, match="10 bytes"),
    ):
        client.download_file("/files/big.bin", max_bytes=10)


def test_a_refused_download_names_the_status():
    handler, _ = _serve(b"forbidden", status=403)
    with (
        Client(CONFIG, transport=httpx2.MockTransport(handler)) as client,
        pytest.raises(FileDownloadError, match="403"),
    ):
        client.download_file("/private/files/r.pdf", max_bytes=10)
