from __future__ import annotations

import asyncio
import socket
import ssl
from typing import Any

import httpcore
import pytest
from backend.app.config import Settings
from backend.app.errors import UnsafeSource
from backend.app.services.rate_sources import (
    SourceFetchError,
    _bounded_headers,
    _PinnedNetworkBackend,
    _request_pinned,
    _WireResponse,
    fetch_official_source,
    validate_official_sce_url,
)


def test_rate_source_rejects_non_https_credentials_ports_hosts_and_fragments() -> None:
    allow = ("www.sce.com", "sce.com")
    for value in (
        "http://www.sce.com/rates",
        "https://user:pass@www.sce.com/rates",
        "https://www.sce.com:8443/rates",
        "https://example.com/rates",
        "https://www.sce.com.evil.test/rates",
        "https://www.sce.com/rates#fragment",
    ):
        with pytest.raises(UnsafeSource):
            validate_official_sce_url(value, allow)

    with pytest.raises(ValueError, match="official SCE hosts"):
        Settings(env="test", allowed_sce_hosts=("www.sce.com", "evil.example"))


def test_rate_source_rejects_private_or_mixed_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    for records in (
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ],
    ):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *_args, _records=records, **_kwargs: _records,
        )
        with pytest.raises(UnsafeSource, match="non-public"):
            validate_official_sce_url("https://www.sce.com/rates", ("www.sce.com",))


@pytest.mark.asyncio
async def test_dns_result_is_bound_to_tcp_peer_without_second_hostname_lookup() -> None:
    calls: list[str] = []

    class ConnectedStream(httpcore.AsyncNetworkStream):
        def get_extra_info(self, info: str) -> object:
            return ("93.184.216.34", 443) if info == "server_addr" else None

    expected_stream = ConnectedStream()

    class Delegate(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore override
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            calls.append(host)
            assert port == 443
            return expected_stream

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore override
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("Unix socket must not be used")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    backend = _PinnedNetworkBackend(
        "www.sce.com",
        ("93.184.216.34", "2001:4860:4860::8888"),
        delegate=Delegate(),
    )
    stream = await backend.connect_tcp("www.sce.com", 443)
    assert stream is expected_stream
    assert calls == ["93.184.216.34"]
    assert backend.connected_ip == "93.184.216.34"


@pytest.mark.asyncio
async def test_pinned_transport_keeps_original_hostname_for_tls_sni_and_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected_hosts: list[str] = []
    tls_hostnames: list[str | None] = []
    writes: list[bytes] = []

    class Stream(httpcore.AsyncNetworkStream):
        def __init__(self) -> None:
            self._response = (
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                b"Content-Length: 2\r\nConnection: close\r\n\r\nok"
            )

        async def read(
            self,
            max_bytes: int,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore override
        ) -> bytes:
            response, self._response = self._response[:max_bytes], self._response[max_bytes:]
            return response

        async def write(
            self,
            buffer: bytes,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore override
        ) -> None:
            if buffer:
                writes.append(buffer)

        async def aclose(self) -> None:
            return None

        async def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore override
        ) -> httpcore.AsyncNetworkStream:
            assert ssl_context.check_hostname is True
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            tls_hostnames.append(server_hostname)
            return self

        def get_extra_info(self, info: str) -> object:
            return ("93.184.216.34", 443) if info == "server_addr" else None

    class Delegate(httpcore.AsyncNetworkBackend):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore override
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            connected_hosts.append(host)
            return Stream()

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,  # noqa: ASYNC109 - httpcore override
            socket_options: Any = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError("Unix socket must not be used")

        async def sleep(self, seconds: float) -> None:
            await asyncio.sleep(seconds)

    monkeypatch.setattr(httpcore, "AnyIOBackend", lambda: Delegate())
    response = await _request_pinned(
        "https://www.sce.com/rates",
        hostname="www.sce.com",
        resolved_ips=("93.184.216.34",),
        headers={"Accept": "text/html"},
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        max_header_bytes=1024,
        max_header_count=20,
        max_body_bytes=1024,
    )
    assert response.body == b"ok"
    assert connected_hosts == ["93.184.216.34"]
    assert tls_hostnames == ["www.sce.com"]
    assert writes and writes[0].startswith(b"GET ")


@pytest.mark.asyncio
async def test_fetch_rejects_connected_peer_outside_prevalidated_dns_set() -> None:
    async def resolver(hostname: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def rebound_request(
        url: str,
        *,
        hostname: str,
        resolved_ips: tuple[str, ...],
        headers: dict[str, str],
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_header_bytes: int,
        max_header_count: int,
        max_body_bytes: int,
    ) -> _WireResponse:
        return _WireResponse(
            status_code=200,
            headers=((b"content-type", b"text/html"),),
            body=b"<html></html>",
            connected_ip="127.0.0.1",
        )

    with pytest.raises(SourceFetchError, match="validated DNS set") as raised:
        await fetch_official_source(
            "https://www.sce.com/rates",
            resolver=resolver,
            request_once=rebound_request,
        )
    assert raised.value.error_code == "PEER_NOT_PINNED"


@pytest.mark.asyncio
async def test_fetch_total_deadline_covers_dns_and_all_redirects() -> None:
    async def stalled_resolver(hostname: str, port: int) -> tuple[str, ...]:
        await asyncio.sleep(0.1)
        return ("93.184.216.34",)

    with pytest.raises(SourceFetchError) as raised:
        await fetch_official_source(
            "https://www.sce.com/rates",
            resolver=stalled_resolver,
            total_timeout_seconds=0.001,
        )
    assert raised.value.error_code == "TOTAL_TIMEOUT"


@pytest.mark.asyncio
async def test_conditional_304_has_no_empty_artifact_hash() -> None:
    seen_headers: dict[str, str] = {}

    async def resolver(hostname: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def not_modified(
        url: str,
        *,
        hostname: str,
        resolved_ips: tuple[str, ...],
        headers: dict[str, str],
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_header_bytes: int,
        max_header_count: int,
        max_body_bytes: int,
    ) -> _WireResponse:
        seen_headers.update(headers)
        return _WireResponse(
            status_code=304,
            headers=(
                (b"etag", b'"revision-1"'),
                (b"last-modified", b"Wed, 12 Aug 2026 20:00:00 GMT"),
            ),
            body=b"",
            connected_ip="93.184.216.34",
        )

    fetched = await fetch_official_source(
        "https://www.sce.com/rates",
        etag='"revision-1"',
        last_modified="Wed, 12 Aug 2026 20:00:00 GMT",
        resolver=resolver,
        request_once=not_modified,
    )
    assert seen_headers["If-None-Match"] == '"revision-1"'
    assert seen_headers["If-Modified-Since"] == "Wed, 12 Aug 2026 20:00:00 GMT"
    assert fetched.status_code == 304
    assert fetched.body is None
    assert fetched.sha256 is None
    assert fetched.byte_count == 0


@pytest.mark.asyncio
async def test_redirect_is_independently_allowlisted() -> None:
    async def resolver(hostname: str, port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    async def redirect(
        url: str,
        *,
        hostname: str,
        resolved_ips: tuple[str, ...],
        headers: dict[str, str],
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        max_header_bytes: int,
        max_header_count: int,
        max_body_bytes: int,
    ) -> _WireResponse:
        return _WireResponse(
            status_code=302,
            headers=((b"location", b"https://evil.example/rates"),),
            body=b"",
            connected_ip="93.184.216.34",
        )

    with pytest.raises(SourceFetchError) as rejected:
        await fetch_official_source(
            "https://www.sce.com/rates",
            resolver=resolver,
            request_once=redirect,
        )
    assert rejected.value.error_code == "HOST_NOT_ALLOWLISTED"


def test_response_header_limits_are_enforced_before_body_processing() -> None:
    with pytest.raises(SourceFetchError) as too_many:
        _bounded_headers([(b"x", b"y")] * 3, max_header_bytes=100, max_header_count=2)
    assert too_many.value.error_code == "HEADERS_TOO_LARGE"

    with pytest.raises(SourceFetchError) as too_large:
        _bounded_headers([(b"x", b"y" * 100)], max_header_bytes=16, max_header_count=2)
    assert too_large.value.error_code == "HEADERS_TOO_LARGE"
