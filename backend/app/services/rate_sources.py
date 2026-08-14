from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpcore

from ..errors import UnsafeSource

DEFAULT_ALLOWED_SCE_HOSTS = ("www.sce.com", "sce.com")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SUCCESS_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml", "application/pdf"})
MAX_DNS_ADDRESSES = 16


class SourceFetchError(UnsafeSource):
    """A typed, safe-to-persist official-source failure."""

    def __init__(
        self,
        error_code: str,
        detail: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.evidence = evidence or {}


@dataclass(frozen=True)
class SourceHop:
    url: str
    hostname: str
    resolved_ips: tuple[str, ...]
    connected_ip: str
    status_code: int
    location: str | None = None


@dataclass(frozen=True)
class SourceFetch:
    requested_url: str
    url: str
    status_code: int
    body: bytes | None
    sha256: str | None
    etag: str | None
    last_modified: str | None
    media_type: str | None
    hops: tuple[SourceHop, ...]

    @property
    def byte_count(self) -> int:
        return len(self.body) if self.body is not None else 0


@dataclass(frozen=True)
class _WireResponse:
    status_code: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes
    connected_ip: str


class _Resolver(Protocol):
    def __call__(self, hostname: str, port: int) -> Awaitable[tuple[str, ...]]: ...


class _RequestOnce(Protocol):
    def __call__(
        self,
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
    ) -> Awaitable[_WireResponse]: ...


def _normalized_allowlist(allowed_hosts: tuple[str, ...]) -> frozenset[str]:
    normalized = frozenset(host.lower().rstrip(".") for host in allowed_hosts)
    if not normalized or any(
        not host
        or ":" in host
        or "/" in host
        or "@" in host
        or host.startswith(".")
        or host.endswith(".")
        for host in normalized
    ):
        raise SourceFetchError("ALLOWLIST_INVALID", "rate-source host allowlist is invalid")
    return normalized


def _validate_url_structure(url: str, allowed_hosts: tuple[str, ...]) -> str:
    if len(url) > 2048 or any(ord(character) < 0x20 for character in url):
        raise SourceFetchError("URL_INVALID", "rate-source URL is malformed")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise SourceFetchError("URL_INVALID", "rate-source URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise SourceFetchError(
            "URL_POLICY_REJECTED",
            "rate sources must use ordinary HTTPS without credentials, fragments, or ports",
        )
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname not in _normalized_allowlist(allowed_hosts):
        raise SourceFetchError("HOST_NOT_ALLOWLISTED", "rate-source hostname is not allowlisted")
    return hostname


def _validated_ip_values(addresses: Iterable[str]) -> tuple[str, ...]:
    values: dict[tuple[int, int], str] = {}
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SourceFetchError(
                "DNS_INVALID", "rate-source DNS returned an invalid address"
            ) from exc
        if not address.is_global:
            raise SourceFetchError(
                "DNS_NON_PUBLIC",
                "rate-source DNS resolved to a non-public address",
            )
        values[(address.version, int(address))] = address.compressed
        if len(values) > MAX_DNS_ADDRESSES:
            raise SourceFetchError(
                "DNS_TOO_MANY_ADDRESSES",
                "rate-source DNS returned too many addresses",
            )
    if not values:
        raise SourceFetchError("DNS_EMPTY", "rate-source DNS returned no usable addresses")
    return tuple(values[key] for key in sorted(values))


def validate_official_sce_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    """Synchronous validation helper retained for configuration and unit checks.

    Fetches use :func:`resolve_public_ips` and then bind the connection to those exact
    results. This helper alone must never be treated as sufficient before a request.
    """

    hostname = _validate_url_structure(url, allowed_hosts)
    try:
        records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceFetchError("DNS_FAILED", "rate-source DNS resolution failed") from exc
    _validated_ip_values(str(record[4][0]) for record in records)
    return hostname


async def resolve_public_ips(hostname: str, port: int = 443) -> tuple[str, ...]:
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise SourceFetchError("DNS_FAILED", "rate-source DNS resolution failed") from exc
    return _validated_ip_values(str(record[4][0]) for record in records)


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to prevalidated IPs while httpcore retains hostname TLS SNI.

    The HTTP request URL remains the original HTTPS hostname. httpcore therefore passes
    that hostname to ``start_tls`` for SNI and certificate verification, but this backend
    replaces the TCP destination with an address from the one-time validated set. No
    second DNS lookup occurs in the transport.
    """

    def __init__(
        self,
        hostname: str,
        resolved_ips: tuple[str, ...],
        *,
        delegate: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._hostname = hostname
        self._resolved_ips = resolved_ips
        self._delegate = delegate or httpcore.AnyIOBackend()
        self.connected_ip: str | None = None

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - required httpcore backend signature
        local_address: str | None = None,
        socket_options: Iterable[tuple[Any, ...]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.lower().rstrip(".") != self._hostname or port != 443:
            raise httpcore.ConnectError("pinned rate-source destination mismatch")
        last_error: Exception | None = None
        for address in self._resolved_ips:
            try:
                stream = await self._delegate.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
                continue
            peer = stream.get_extra_info("server_addr")
            peer_value = peer[0] if isinstance(peer, tuple | list) and peer else None
            try:
                peer_ip = ipaddress.ip_address(str(peer_value)).compressed
            except ValueError:
                await stream.aclose()
                last_error = httpcore.ConnectError(
                    "pinned rate-source transport did not expose a valid peer"
                )
                continue
            if peer_ip != address:
                await stream.aclose()
                last_error = httpcore.ConnectError(
                    "pinned rate-source peer differed from the requested address"
                )
                continue
            self.connected_ip = peer_ip
            return stream
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("no pinned rate-source destination was available")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - required httpcore backend signature
        socket_options: Iterable[tuple[Any, ...]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are forbidden for official rate sources")

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


def _bounded_headers(
    headers: list[tuple[bytes, bytes]], *, max_header_bytes: int, max_header_count: int
) -> tuple[tuple[bytes, bytes], ...]:
    if len(headers) > max_header_count:
        raise SourceFetchError("HEADERS_TOO_LARGE", "rate-source response has too many headers")
    size = sum(len(name) + len(value) + 4 for name, value in headers)
    if size > max_header_bytes:
        raise SourceFetchError("HEADERS_TOO_LARGE", "rate-source response headers are too large")
    return tuple(headers)


def _header_values(headers: tuple[tuple[bytes, bytes], ...], name: str) -> tuple[str, ...]:
    expected = name.lower().encode("ascii")
    try:
        return tuple(
            value.decode("latin-1").strip() for key, value in headers if key.lower() == expected
        )
    except UnicodeDecodeError as exc:  # pragma: no cover - latin-1 accepts all bytes
        raise SourceFetchError("HEADER_INVALID", "rate-source response header is invalid") from exc


def _single_header(
    headers: tuple[tuple[bytes, bytes], ...], name: str, *, required: bool = False
) -> str | None:
    values = _header_values(headers, name)
    if len(values) > 1:
        raise SourceFetchError("HEADER_AMBIGUOUS", f"rate-source {name} header is ambiguous")
    if required and not values:
        raise SourceFetchError("HEADER_MISSING", f"rate-source {name} header is required")
    return values[0] if values else None


def _bounded_header_value(
    headers: tuple[tuple[bytes, bytes], ...],
    name: str,
    *,
    max_length: int,
) -> str | None:
    value = _single_header(headers, name)
    if value is not None and (
        len(value) > max_length or any(ord(character) < 0x20 for character in value)
    ):
        raise SourceFetchError("HEADER_INVALID", f"rate-source {name} header is invalid")
    return value


async def _request_pinned(
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
    backend = _PinnedNetworkBackend(hostname, resolved_ips)
    context = ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    pool = httpcore.AsyncConnectionPool(
        ssl_context=context,
        max_connections=1,
        max_keepalive_connections=0,
        http1=True,
        http2=False,
        retries=0,
        network_backend=backend,
    )
    extensions = {
        "timeout": {
            "connect": connect_timeout_seconds,
            "read": read_timeout_seconds,
            "write": connect_timeout_seconds,
            "pool": connect_timeout_seconds,
        }
    }
    try:
        async with pool.stream(
            "GET", url, headers=list(headers.items()), extensions=extensions
        ) as response:
            response_headers = _bounded_headers(
                response.headers,
                max_header_bytes=max_header_bytes,
                max_header_count=max_header_count,
            )
            content_length = _single_header(response_headers, "content-length")
            if content_length is not None and response.status == 200:
                try:
                    declared_size = int(content_length, 10)
                except ValueError as exc:
                    raise SourceFetchError(
                        "CONTENT_LENGTH_INVALID",
                        "rate-source Content-Length is invalid",
                    ) from exc
                if declared_size < 0 or declared_size > max_body_bytes:
                    raise SourceFetchError(
                        "BODY_TOO_LARGE",
                        "rate-source response exceeds the configured limit",
                    )
            chunks: list[bytes] = []
            size = 0
            if response.status == 200:
                async for chunk in response.aiter_stream():
                    size += len(chunk)
                    if size > max_body_bytes:
                        raise SourceFetchError(
                            "BODY_TOO_LARGE",
                            "rate-source response exceeds the configured limit",
                        )
                    chunks.append(chunk)
            connected_ip = backend.connected_ip
            if connected_ip is None:
                raise SourceFetchError(
                    "PEER_NOT_PINNED",
                    "rate-source transport did not report a pinned peer",
                )
            return _WireResponse(
                status_code=response.status,
                headers=response_headers,
                body=b"".join(chunks),
                connected_ip=connected_ip,
            )
    finally:
        await pool.aclose()


def _media_type(headers: tuple[tuple[bytes, bytes], ...]) -> str:
    content_type = _single_header(headers, "content-type", required=True)
    assert content_type is not None
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in SUCCESS_MEDIA_TYPES:
        raise SourceFetchError(
            "CONTENT_TYPE_REJECTED",
            "rate-source response Content-Type is not permitted",
        )
    encoding = _single_header(headers, "content-encoding")
    if encoding is not None and encoding.lower() not in ("", "identity"):
        raise SourceFetchError(
            "CONTENT_ENCODING_REJECTED",
            "compressed rate-source responses are not accepted",
        )
    return media_type


async def fetch_official_source(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_SCE_HOSTS,
    etag: str | None = None,
    last_modified: str | None = None,
    max_bytes: int = 5_000_000,
    max_redirects: int = 3,
    connect_timeout_seconds: float = 5.0,
    read_timeout_seconds: float = 15.0,
    total_timeout_seconds: float = 30.0,
    max_header_bytes: int = 65_536,
    max_header_count: int = 100,
    resolver: _Resolver | None = None,
    request_once: _RequestOnce | None = None,
) -> SourceFetch:
    """Fetch one official source with DNS-to-socket binding and bounded resources."""

    if (
        max_bytes < 1
        or max_redirects < 0
        or connect_timeout_seconds <= 0
        or read_timeout_seconds <= 0
        or total_timeout_seconds <= 0
        or max_header_bytes < 1
        or max_header_count < 1
    ):
        raise ValueError("official-source resource limits must be positive")
    requested_url = url
    current = url
    resolve = resolver or resolve_public_ips
    request = request_once or _request_pinned
    request_headers: dict[str, str] = {
        "Accept": "text/html,application/xhtml+xml,application/pdf",
        "Accept-Encoding": "identity",
        "User-Agent": "PowerMeter-V2-RateSync/1.0",
    }
    if etag is not None and (len(etag) > 300 or any(ord(character) < 0x20 for character in etag)):
        raise SourceFetchError("VALIDATOR_INVALID", "stored rate-source ETag is invalid")
    if last_modified is not None and (
        len(last_modified) > 200 or any(ord(character) < 0x20 for character in last_modified)
    ):
        raise SourceFetchError(
            "VALIDATOR_INVALID",
            "stored rate-source Last-Modified value is invalid",
        )
    if etag:
        request_headers["If-None-Match"] = etag
    if last_modified:
        request_headers["If-Modified-Since"] = last_modified

    async def perform() -> SourceFetch:
        hops: list[SourceHop] = []
        nonlocal current
        try:
            for redirect_count in range(max_redirects + 1):
                hostname = _validate_url_structure(current, allowed_hosts)
                resolved_ips = await resolve(hostname, 443)
                wire = await request(
                    current,
                    hostname=hostname,
                    resolved_ips=resolved_ips,
                    headers=request_headers,
                    connect_timeout_seconds=connect_timeout_seconds,
                    read_timeout_seconds=read_timeout_seconds,
                    max_header_bytes=max_header_bytes,
                    max_header_count=max_header_count,
                    max_body_bytes=max_bytes,
                )
                if wire.connected_ip not in resolved_ips:
                    raise SourceFetchError(
                        "PEER_NOT_PINNED",
                        "rate-source connection peer was not in the validated DNS set",
                    )
                location = (
                    _single_header(wire.headers, "location")
                    if wire.status_code in REDIRECT_STATUSES
                    else None
                )
                hops.append(
                    SourceHop(
                        url=current,
                        hostname=hostname,
                        resolved_ips=resolved_ips,
                        connected_ip=wire.connected_ip,
                        status_code=wire.status_code,
                        location=location,
                    )
                )
                if wire.status_code in REDIRECT_STATUSES:
                    if redirect_count == max_redirects:
                        raise SourceFetchError(
                            "REDIRECT_LIMIT",
                            "rate source exceeded the redirect limit",
                        )
                    if not location:
                        raise SourceFetchError(
                            "REDIRECT_MISSING_LOCATION",
                            "rate source returned an empty redirect",
                        )
                    redirected = urljoin(current, location)
                    _validate_url_structure(redirected, allowed_hosts)
                    current = redirected
                    continue
                response_etag = _bounded_header_value(wire.headers, "etag", max_length=300) or etag
                response_last_modified = (
                    _bounded_header_value(wire.headers, "last-modified", max_length=200)
                    or last_modified
                )
                if wire.status_code == 304:
                    if etag is None and last_modified is None:
                        raise SourceFetchError(
                            "UNSOLICITED_NOT_MODIFIED",
                            "rate source returned 304 without a conditional request",
                        )
                    return SourceFetch(
                        requested_url=requested_url,
                        url=current,
                        status_code=304,
                        body=None,
                        sha256=None,
                        etag=response_etag,
                        last_modified=response_last_modified,
                        media_type=None,
                        hops=tuple(hops),
                    )
                if wire.status_code != 200:
                    raise SourceFetchError(
                        "HTTP_STATUS_REJECTED",
                        f"rate source returned HTTP {wire.status_code}",
                    )
                media_type = _media_type(wire.headers)
                digest = hashlib.sha256(wire.body).hexdigest()
                return SourceFetch(
                    requested_url=requested_url,
                    url=current,
                    status_code=200,
                    body=wire.body,
                    sha256=digest,
                    etag=response_etag,
                    last_modified=response_last_modified,
                    media_type=media_type,
                    hops=tuple(hops),
                )
        except SourceFetchError as exc:
            if hops and "hops" not in exc.evidence:
                exc.evidence["hops"] = [
                    {
                        "url": hop.url,
                        "hostname": hop.hostname,
                        "resolved_ips": list(hop.resolved_ips),
                        "connected_ip": hop.connected_ip,
                        "status_code": hop.status_code,
                        "location": hop.location,
                    }
                    for hop in hops
                ]
            raise
        raise SourceFetchError("FETCH_INCOMPLETE", "rate-source fetch did not complete")

    try:
        async with asyncio.timeout(total_timeout_seconds):
            return await perform()
    except TimeoutError as exc:
        raise SourceFetchError(
            "TOTAL_TIMEOUT",
            "rate-source fetch exceeded the total deadline",
        ) from exc
    except httpcore.ConnectTimeout as exc:
        raise SourceFetchError("CONNECT_TIMEOUT", "rate-source connection timed out") from exc
    except httpcore.ReadTimeout as exc:
        raise SourceFetchError("READ_TIMEOUT", "rate-source response timed out") from exc
    except httpcore.WriteTimeout as exc:
        raise SourceFetchError("WRITE_TIMEOUT", "rate-source request timed out") from exc
    except httpcore.PoolTimeout as exc:
        raise SourceFetchError("POOL_TIMEOUT", "rate-source connection pool timed out") from exc
    except httpcore.ConnectError as exc:
        raise SourceFetchError("CONNECT_FAILED", "rate-source connection failed") from exc
    except httpcore.ReadError as exc:
        raise SourceFetchError("READ_FAILED", "rate-source response read failed") from exc
    except httpcore.WriteError as exc:
        raise SourceFetchError("WRITE_FAILED", "rate-source request write failed") from exc
    except (httpcore.LocalProtocolError, httpcore.RemoteProtocolError) as exc:
        raise SourceFetchError(
            "HTTP_PROTOCOL_ERROR", "rate-source HTTP response was invalid"
        ) from exc
    except httpcore.NetworkError as exc:
        raise SourceFetchError("NETWORK_FAILED", "rate-source network operation failed") from exc


__all__ = [
    "DEFAULT_ALLOWED_SCE_HOSTS",
    "SourceFetch",
    "SourceFetchError",
    "SourceHop",
    "fetch_official_source",
    "resolve_public_ips",
    "validate_official_sce_url",
]
