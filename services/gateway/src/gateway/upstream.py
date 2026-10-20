import socket
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .ssrf import HostResolutionError, UnsafeUrlError, check_upstream_url


@dataclass(frozen=True)
class Target:
    """Everything needed to open one connection to an upstream; the credential is already decrypted."""

    slug: str
    url: str
    auth_type: str
    auth_header_name: str | None
    credential: str | None
    timeout_seconds: float


Connector = Callable[[Target], AbstractAsyncContextManager[Client]]


def auth_headers(target: Target) -> dict[str, str]:
    """The gateway's own credential for this upstream; the caller's key is never forwarded."""
    if target.auth_type == "bearer" and target.credential:
        return {"Authorization": f"Bearer {target.credential}"}
    if target.auth_type == "header" and target.credential and target.auth_header_name:
        return {target.auth_header_name: target.credential}
    return {}


def http_connector(allowed_internal_hosts: list[str]) -> Connector:
    @asynccontextmanager
    async def connect(target: Target) -> AsyncIterator[Client]:
        await check_upstream_url(target.url, allowed_internal_hosts)
        timeout = httpx2.Timeout(target.timeout_seconds, connect=min(3.0, target.timeout_seconds))
        async with httpx2.AsyncClient(headers=auth_headers(target), timeout=timeout, follow_redirects=False) as http:
            async with Client(
                streamable_http_client(target.url, http_client=http),
                read_timeout_seconds=target.timeout_seconds,
            ) as client:
                yield client

    return connect


def _leaf(exc: BaseException) -> BaseException:
    leaf = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]
    return leaf


def classify_error(exc: BaseException) -> str:
    """Return one bounded failure class suitable for metrics and the dashboard."""
    leaf = _leaf(exc)
    chain: BaseException | None = leaf
    while chain is not None:
        if isinstance(chain, HostResolutionError | socket.gaierror):
            return "dns"
        if isinstance(chain, UnsafeUrlError):
            return "blocked"
        if isinstance(chain, ssl.SSLError):
            return "tls"
        chain = chain.__cause__ or chain.__context__
    if isinstance(leaf, TimeoutError | httpx2.TimeoutException):
        return "timeout"
    if isinstance(leaf, httpx2.HTTPStatusError):
        return "auth" if leaf.response.status_code in (401, 403) else "http"
    if isinstance(leaf, httpx2.ConnectError):
        text = str(leaf).lower()
        if "name" in text and ("resolve" in text or "known" in text or "not found" in text):
            return "dns"
        if "ssl" in text or "certificate" in text or "tls" in text:
            return "tls"
        return "refused"
    return "protocol"


def describe_error(exc: BaseException) -> str:
    """Short, classified reason shown in the dashboard and to models."""
    leaf = _leaf(exc)
    failure = classify_error(exc)
    if failure == "dns":
        if isinstance(leaf, HostResolutionError):
            return str(leaf)
        return "upstream name does not resolve"
    if failure == "blocked":
        return f"URL blocked by SSRF guard: {leaf}"
    if failure == "tls":
        return "TLS certificate is not valid for the upstream host"
    if isinstance(leaf, TimeoutError | httpx2.TimeoutException):
        return "no response before the timeout"
    if failure == "refused":
        return "connection refused (pod down or Service has no ready endpoints)"
    if isinstance(leaf, httpx2.HTTPStatusError):
        code = leaf.response.status_code
        if failure == "auth":
            return f"upstream rejected the gateway credential (HTTP {code})"
        return f"upstream answered HTTP {code}"
    message = str(leaf) or type(leaf).__name__
    return f"not an MCP Streamable HTTP endpoint: {message[:160]}"
