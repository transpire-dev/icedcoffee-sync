"""The URL checks every ERPNext site URL passes, allowlist or not."""

import pytest
from icedcoffee_sync import site


def test_userinfo_is_refused_before_anything_and_never_echoed() -> None:
    with pytest.raises(site.SiteUrlRefusedError) as excinfo:
        site.require_safe_site_url("https://alice:hunter2@shop.example.com")
    assert "hunter2" not in str(excinfo.value)
    assert "credential" in str(excinfo.value)


LEDGER = {"ledger.example": "the ledger"}


def test_a_refused_host_is_refused_by_the_name_the_caller_gave_it() -> None:
    with pytest.raises(site.SiteUrlRefusedError) as excinfo:
        site.require_safe_site_url("https://ledger.example.", refused_hosts=LEDGER)
    assert str(excinfo.value) == "refusing the ledger 'ledger.example'"


def test_a_fully_qualified_refused_host_is_still_refused() -> None:
    with pytest.raises(site.SiteUrlRefusedError, match="the ledger"):
        site.require_safe_site_url("https://ledger.example./x", refused_hosts=LEDGER)


def test_the_library_does_not_know_any_ledger_host() -> None:
    assert "BOOKS_SITE_HOST" not in dir(site)


def test_plain_http_is_refused() -> None:
    with pytest.raises(site.SiteUrlRefusedError, match="https"):
        site.require_safe_site_url("http://shop.example.com")


@pytest.mark.parametrize(
    "url", ["https://shop.example.com:8443", "https://shop.example.com:not-a-port", "https://shop.example.com:65536"]
)
def test_a_port_other_than_443_is_refused(url: str) -> None:
    with pytest.raises(site.SiteUrlRefusedError, match="port"):
        site.require_safe_site_url(url)


def test_an_explicit_443_passes() -> None:
    site.require_safe_site_url("https://shop.example.com:443")


def test_any_https_host_passes_with_no_refused_hosts() -> None:
    site.require_safe_site_url("https://customer-erp.example.com")


def test_hostname_lowercases_and_strips_the_trailing_dot() -> None:
    assert site.hostname("https://Shop.Example.COM./api") == "shop.example.com"


@pytest.mark.parametrize("url", ["https:///path", "https://", "https://:443"])
def test_a_url_with_no_hostname_is_refused(url: str) -> None:
    with pytest.raises(site.SiteUrlRefusedError, match="no hostname"):
        site.require_safe_site_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://shop.example.com/api/v2", "https://shop.example.com/?x=1", "https://shop.example.com/#frag"],
)
def test_anything_past_the_site_root_is_refused(url: str) -> None:
    with pytest.raises(site.SiteUrlRefusedError, match="root"):
        site.require_safe_site_url(url)


def test_a_trailing_slash_is_still_the_root() -> None:
    site.require_safe_site_url("https://shop.example.com/")
