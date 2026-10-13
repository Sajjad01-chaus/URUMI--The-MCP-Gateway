import asyncio
import fnmatch
import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """The URL breaks the upstream URL policy (scheme, credentials in URL, forbidden address)."""


class HostResolutionError(UnsafeUrlError):
    """The host name does not resolve (not a policy violation; the upstream is just unreachable)."""


ALWAYS_BLOCKED = [
    ipaddress.ip_network(n)
    for n in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
              "::/128", "::1/128", "fe80::/10", "ff00::/8")
]
PRIVATE = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
]


async def check_upstream_url(url: str, allowed_internal_hosts: list[str]) -> None:
    """Reject URLs that could make the gateway reach places it must not (SSRF).

    Private addresses are allowed only for host names on the trusted allow-list
    (the in-cluster MCP services). Called on create/edit and before every connection,
    so a DNS answer that changes later (DNS rebinding) is caught too.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError("only http:// and https:// URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeUrlError("credentials must not be embedded in the URL; use the credential field")
    if parts.fragment:
        raise UnsafeUrlError("URL fragments are not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("URL has no host")

    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrlError("invalid port") from exc

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HostResolutionError(f"host '{host}' does not resolve") from exc

    trusted_internal = any(fnmatch.fnmatch(host, pattern.lower()) for pattern in allowed_internal_hosts)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if any(ip in net for net in ALWAYS_BLOCKED):
            raise UnsafeUrlError(f"'{host}' resolves to {ip} (loopback, link-local/cloud metadata or reserved)")
        if any(ip in net for net in PRIVATE) and not trusted_internal:
            raise UnsafeUrlError(
                f"'{host}' resolves to private address {ip}; internal hosts must match the allow-list"
            )
