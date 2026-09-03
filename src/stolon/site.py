"""The checks every ERPNext site URL passes before a credential is sent to it.

No allowlist lives here. A connector that only reaches allowlisted hosts
connects to nobody; a demo-data tool that reaches any host is a hazard. So
this module refuses what is always wrong -- a credential in the URL, no
hostname, plain HTTP, a port that is not 443, anything past the site's root,
and any host the caller names as refused -- and
leaves "which hosts may I write to" to the caller.

Refusal order is deliberate: userinfo first, because every later message
quotes the URL back and a password typed into it must never reach stderr.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# The real ledger. Named here so every caller refuses it the same way.
BOOKS_SITE_HOST = "transpire.frappe.cloud"

HTTPS_PORT = 443


class SiteUrlRefusedError(ValueError):
    """The URL is one no ERPNext credential should be sent to."""


def hostname(url: str) -> str:
    """The URL's host, lowercased, without a trailing dot.

    `host.` resolves to the same site as `host`; an equality check that
    missed the dot would let a refused host through.
    """
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def require_safe_site_url(url: str, *, refused_hosts: frozenset[str] = frozenset()) -> None:
    """Refuse a URL that carries a credential, is not https on 443, or names a refused host.

    Args:
        url: The site URL as the operator typed it.
        refused_hosts: Hosts refused by name, ahead of every other check, so
            the message says why rather than "not allowed". Callers pass the
            books site here.

    Raises:
        SiteUrlRefusedError: With a message that never contains userinfo.
    """
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise SiteUrlRefusedError("refusing a site URL carrying a credential; pass the key and secret separately")
    host = hostname(url)
    if not host:
        raise SiteUrlRefusedError(f"refusing {url!r}: no hostname")
    if host in refused_hosts:
        label = "the books site" if host == BOOKS_SITE_HOST else "a refused host"
        raise SiteUrlRefusedError(f"refusing {label} {host!r}")
    if parts.scheme != "https":
        raise SiteUrlRefusedError(f"refusing {url!r}: the API key rides in a header, so the scheme must be https")
    try:
        port = parts.port
    except ValueError:
        raise SiteUrlRefusedError(f"refusing {url!r}: unreadable port") from None
    if port not in (None, HTTPS_PORT):
        raise SiteUrlRefusedError(f"refusing {url!r}: port {port} is not {HTTPS_PORT}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise SiteUrlRefusedError(f"refusing {url!r}: a site URL is the site's root, with no path, query, or fragment")
