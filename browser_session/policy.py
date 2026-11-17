"""Where a session may navigate.

Checked for every document request (top-level pages and iframes) the browser
makes, not only for `goto`: links, redirects and scripts can't leave the
allow-list either. Private, loopback and link-local addresses are refused
unless the service runs with BROWSER_ALLOW_PRIVATE=1, so a session can't be
pointed at the host's own services.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

ALWAYS_ALLOWED = ("about:blank",)


class PolicyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_domains(raw: object) -> list[str]:
    """A list (or comma-separated text) of domains; '*' means any."""
    if raw is None or raw == "":
        return []
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise PolicyError("invalid_domains", "allow_domains is a list of domain names")
    out = []
    for item in items:
        if not isinstance(item, str):
            raise PolicyError("invalid_domains", "allow_domains is a list of domain names")
        item = item.strip().lower().lstrip(".")
        if item.startswith("*."):
            item = item[2:]
        if not item:
            continue
        if item != "*" and (len(item) > 253 or any(c in item for c in "/:@ ")):
            raise PolicyError("invalid_domains", f"not a domain name: {item!r}")
        out.append(item)
    return out[:100]


def effective_domains(requested: list[str], service: list[str]) -> list[str]:
    """A session may only narrow the service-wide allow-list."""
    if not service or "*" in service:
        return requested or ["*"]
    if not requested or "*" in requested:
        return service
    narrowed = [d for d in requested if domain_allowed(d, service)]
    if not narrowed:
        raise PolicyError("domain_not_allowed", "none of the requested domains is allowed by this service")
    return narrowed


def domain_allowed(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(d == "*" or host == d or host.endswith("." + d) for d in domains)


def _private(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not addr.is_global


def check_url_shape(url: str, domains: list[str]) -> tuple[str, str]:
    """Scheme and host checks that need no DNS. Returns (scheme, host)."""
    if url in ALWAYS_ALLOWED:
        return "about", ""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise PolicyError("invalid_url", "not a valid URL") from None
    if parts.scheme not in ("http", "https"):
        raise PolicyError("scheme_not_allowed", f"only http and https pages (got {parts.scheme or 'none'})")
    if parts.username or parts.password:
        raise PolicyError("invalid_url", "URLs with credentials are refused")
    host = (parts.hostname or "").lower()
    if not host:
        raise PolicyError("invalid_url", "the URL has no host")
    if not domain_allowed(host, domains):
        raise PolicyError("domain_not_allowed", f"{host} is not in this session's allowed domains")
    return parts.scheme, host


async def check_url(url: str, domains: list[str], allow_private: bool) -> None:
    scheme, host = check_url_shape(url, domains)
    if scheme == "about" or allow_private:
        return
    try:
        ipaddress.ip_address(host.strip("[]"))
        addresses = [host.strip("[]")]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return  # unresolvable: the browser will fail on its own
        addresses = [info[4][0] for info in infos]
    if any(_private(a.split("%")[0]) for a in addresses):
        raise PolicyError("private_address", f"{host} resolves to a private address")
