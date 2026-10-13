from dataclasses import dataclass
from typing import Any

import httpx


class WooError(Exception):
    """A WooCommerce call failed; the message is safe to show to users and models."""


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int
    total_pages: int


class WooClient:
    """Async client for the WooCommerce REST API (v3), reached over in-cluster HTTP."""

    def __init__(
        self,
        base_url: str,
        store_host: str,
        consumer_key: str,
        consumer_secret: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        # WordPress trusts X-Forwarded-Proto (set in wp-config), so WooCommerce accepts Basic auth on plain HTTP.
        # Host must be the public store host: WordPress builds URLs and checks requests against it.
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/wp-json",
            auth=(consumer_key, consumer_secret),
            headers={"X-Forwarded-Proto": "https", "Host": store_host, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        response = await self._send(method, path, params=params, json=json)
        return response.json()

    async def get_page(self, path: str, params: dict) -> Page:
        response = await self._send("GET", path, params=params)
        return Page(
            items=response.json(),
            total=int(response.headers.get("X-WP-Total", 0)),
            total_pages=int(response.headers.get("X-WP-TotalPages", 0)),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise WooError("WooCommerce did not respond in time") from exc
        except httpx.HTTPError as exc:
            raise WooError(f"Cannot reach WooCommerce ({type(exc).__name__})") from exc

        if response.status_code >= 400:
            raise WooError(_describe_error(response))
        return response


def _describe_error(response: httpx.Response) -> str:
    try:
        message = response.json().get("message") or response.text
    except ValueError:
        message = response.text
    return f"WooCommerce returned HTTP {response.status_code}: {message[:300]}"
