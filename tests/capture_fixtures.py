"""Capture real Frappe v2 responses as fixtures. Run by hand, not by pytest.

Usage:
    ICEDCOFFEE_TEST_URL=https://<disposable-site>.frappe.cloud \
    ICEDCOFFEE_TEST_KEY=... ICEDCOFFEE_TEST_SECRET=... \
    uv run python tests/capture_fixtures.py

Writes one file per scenario into tests/fixtures/, containing the status code,
response headers we care about, and the raw body verbatim. Nothing is
normalised on the way in.

Two of the scenarios are named for what they actually captured rather than
what a first guess might have provoked: this API key is broadly privileged on
the test site, so the "permission" request succeeds instead of being denied;
and the "duplicate" request fails ERPNext's root-account validation before it
ever reaches duplicate-name detection. Both are documented as such in
tests/fixtures/README.md rather than forced into a misleading name.
"""

import json
import os
import pathlib
import sys
import urllib.parse

import httpx2

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Mirrors the guard in tests/test_live_frappe.py: this script writes junk
# documents to whatever ICEDCOFFEE_TEST_URL points at, so it must refuse the
# books site before sending a single request. Matches the hostname only --
# a custom-domain alias pointed at the same site would not be caught, so
# don't over-trust it as a complete guard.
BOOKS_SITE = "transpire.frappe.cloud"


def _require_not_books_site(url: str) -> None:
    # Normalise before comparing: a fully-qualified `host.` with a trailing dot
    # resolves to the same site but would not match a literal equality check.
    hostname = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    if hostname == BOOKS_SITE:
        sys.exit(f"refusing to run against the books site: {url}")


def main() -> None:
    base = os.environ["ICEDCOFFEE_TEST_URL"].rstrip("/")
    _require_not_books_site(base)
    auth = f"token {os.environ['ICEDCOFFEE_TEST_KEY']}:{os.environ['ICEDCOFFEE_TEST_SECRET']}"
    headers = {"Authorization": auth, "Accept": "application/json"}
    FIXTURES.mkdir(exist_ok=True)

    with httpx2.Client(timeout=30.0) as client:
        # Bootstrap: list one real Account so get_ok can fetch it by name.
        # Account names on this site contain spaces and parentheses (e.g.
        # "Application of Funds (Assets) - AB"), so the name must be
        # URL-quoted before it goes in a path segment.
        bootstrap = client.request(
            "GET",
            f"{base}/api/v2/document/Account?limit=1",
            headers=headers,
        )
        bootstrap_name = bootstrap.json()["data"][0]["name"]
        quoted_name = urllib.parse.quote(bootstrap_name, safe="")

        scenarios = [
            ("list_ok", "GET", "/api/v2/document/Account?limit=5", None),
            ("get_ok", "GET", f"/api/v2/document/Account/{quoted_name}", None),
            ("error_not_found", "GET", "/api/v2/document/Account/NoSuchAccount-XYZ", None),
            ("permission_not_provoked", "GET", "/api/v2/document/User?limit=1", None),
            ("error_validation", "POST", "/api/v2/document/Account", {"account_name": ""}),
            ("duplicate_not_provoked", "POST", "/api/v2/document/Account", {"account_name": "Cash"}),
        ]

        for name, method, path, body in scenarios:
            response = client.request(
                method,
                f"{base}{path}",
                headers=headers,
                json=body,
            )
            payload = {
                "scenario": name,
                "method": method,
                "path": path,
                "sent": body,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "body": response.text,
            }
            (FIXTURES / f"{name}.json").write_text(json.dumps(payload, indent=2))
            print(f"{name:20} {response.status_code}")


if __name__ == "__main__":
    main()
