"""Tests for SSRF-safe fetching (mocked transport, injected resolver)."""

from __future__ import annotations

import httpx
import pytest

from jobguard.sources.http import (
    FetchBlockedIp,
    FetchDenied,
    FetchFailed,
    FetchTooLarge,
    fetch_url_safe,
)

ALLOW = ("boards-api.greenhouse.io",)


def _client(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def test_ok_fetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"jobs": []}')

    body = fetch_url_safe(
        "https://boards-api.greenhouse.io/v1/boards/x/jobs",
        allowed_domains=ALLOW,
        client=_client(handler),
        resolve=lambda host: ["93.184.216.34"],
    )
    assert body == b'{"jobs": []}'


def test_non_allowlisted_domain_denied_without_network() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit network")

    with pytest.raises(FetchDenied):
        fetch_url_safe(
            "https://evil.example/jobs",
            allowed_domains=ALLOW,
            client=_client(handler),
            resolve=lambda host: ["93.184.216.34"],
        )


def test_subdomain_spoof_denied() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit network")

    with pytest.raises(FetchDenied):
        fetch_url_safe(
            "https://boards-api.greenhouse.io.evil.example/",
            allowed_domains=ALLOW,
            client=_client(handler),
            resolve=lambda host: ["93.184.216.34"],
        )


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
def test_non_public_ip_blocked(ip: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("must not hit network")

    with pytest.raises(FetchBlockedIp):
        fetch_url_safe(
            "https://boards-api.greenhouse.io/",
            allowed_domains=ALLOW,
            client=_client(handler),
            resolve=lambda host: [ip],
        )


def test_non_http_scheme_denied() -> None:
    with pytest.raises(FetchDenied):
        fetch_url_safe(
            "file:///etc/passwd",
            allowed_domains=ALLOW,
            resolve=lambda host: ["93.184.216.34"],
        )


def test_too_large_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    with pytest.raises(FetchTooLarge):
        fetch_url_safe(
            "https://boards-api.greenhouse.io/",
            allowed_domains=ALLOW,
            max_bytes=10,
            client=_client(handler),
            resolve=lambda host: ["93.184.216.34"],
        )


def test_http_error_surfaces() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(FetchFailed):
        fetch_url_safe(
            "https://boards-api.greenhouse.io/",
            allowed_domains=ALLOW,
            client=_client(handler),
            resolve=lambda host: ["93.184.216.34"],
        )


def test_redirect_off_allowlist_denied() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    with pytest.raises(FetchDenied):
        fetch_url_safe(
            "https://boards-api.greenhouse.io/start",
            allowed_domains=ALLOW,
            client=_client(handler),
            resolve=lambda host: ["93.184.216.34"],
        )


def test_redirect_on_allowlist_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"location": "https://boards-api.greenhouse.io/final"}
            )
        return httpx.Response(200, content=b"done")

    body = fetch_url_safe(
        "https://boards-api.greenhouse.io/start",
        allowed_domains=ALLOW,
        client=_client(handler),
        resolve=lambda host: ["93.184.216.34"],
    )
    assert body == b"done"
