"""SSRF-safe HTTP fetch shared by board and RSS adapters.

Every outbound fetch goes through :func:`fetch_url_safe`, which enforces:
- http/https only (no file/gopher/dict schemes),
- target host on an explicit domain allowlist,
- DNS resolves to public IPs only (private, loopback, link-local, multicast
  and reserved ranges are rejected — checked on *every* resolved address to
  blunt DNS-rebinding),
- bounded time and size (timeout + max-bytes cap),
- redirects followed only while the next host stays allowlisted (max 3 hops).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Collection
from urllib.parse import urlparse

import httpx

Resolver = Callable[[str], list[str]]


class FetchDenied(RuntimeError):
    """URL failed allowlist / scheme / redirect checks."""


class FetchBlockedIp(RuntimeError):
    """URL resolved to a non-public IP address."""


class FetchTooLarge(RuntimeError):
    """Response exceeded the byte cap."""


class FetchFailed(RuntimeError):
    """Transport-level failure (timeout, 5xx, invalid body)."""


def default_resolve(host: str) -> list[str]:
    """Resolve ``host`` to IP strings via the system resolver."""
    infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
    return list({str(info[4][0]) for info in infos})


def _check_public(resolved: list[str], url: str) -> None:
    if not resolved:
        raise FetchBlockedIp(f"No DNS records for {url}")
    for addr in resolved:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise FetchBlockedIp(f"Unparseable address {addr!r} for {url}") from exc
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise FetchBlockedIp(f"Non-public IP {ip} for {url}")


def _host_allowed(host: str, allowed_domains: Collection[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == dom or host.endswith("." + dom) for dom in allowed_domains)


def fetch_url_safe(
    url: str,
    *,
    allowed_domains: Collection[str],
    timeout_s: float = 10.0,
    max_bytes: int = 1_000_000,
    max_redirects: int = 3,
    client: httpx.Client | None = None,
    resolve: Resolver | None = None,
) -> bytes:
    """Fetch ``url`` with SSRF guards. Returns the body (capped)."""
    resolve = resolve or default_resolve
    current = url
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise FetchDenied(f"Unsupported URL: {current!r}")
        if not _host_allowed(parsed.hostname, allowed_domains):
            raise FetchDenied(f"Domain not allowlisted: {parsed.hostname!r}")
        _check_public(resolve(parsed.hostname), current)

        own_client = client is None
        http = client or httpx.Client(timeout=timeout_s, follow_redirects=False)
        try:
            try:
                resp = http.get(current, headers={"User-Agent": "JobGuard/0.1"})
            except httpx.HTTPError as exc:
                raise FetchFailed(f"Fetch failed for {current!r}: {exc}") from exc
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if not location:
                    raise FetchFailed(f"Redirect without location from {current!r}")
                current = location
                continue
            if resp.status_code >= 400:
                raise FetchFailed(f"HTTP {resp.status_code} for {current!r}")
            body = resp.content
            if len(body) > max_bytes:
                raise FetchTooLarge(f"Body exceeds {max_bytes} bytes for {current!r}")
            return body
        finally:
            if own_client:
                http.close()
    raise FetchDenied(f"Too many redirects starting from {url!r}")
