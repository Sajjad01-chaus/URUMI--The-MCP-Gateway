from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .ssrf import check_upstream_url


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


def describe_error(exc: BaseException) -> str:
    """Short, human-readable reason an upstream call failed (shown in the dashboard and to models)."""
    leaf = exc
    while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
        leaf = leaf.exceptions[0]
    if isinstance(leaf, TimeoutError | httpx2.TimeoutException):
        return "timed out"
    if isinstance(leaf, httpx2.ConnectError):
        text = str(leaf).lower()
        if "name" in text and ("resolve" in text or "known" in text or "not found" in text):
            return "DNS lookup failed"
        return "connection refused or host unreachable"
    if isinstance(leaf, httpx2.HTTPStatusError):
        code = leaf.response.status_code
        hint = " (check the upstream credential)" if code in (401, 403) else ""
        return f"upstream answered HTTP {code}{hint}"
    message = str(leaf) or type(leaf).__name__
    return message[:200]
