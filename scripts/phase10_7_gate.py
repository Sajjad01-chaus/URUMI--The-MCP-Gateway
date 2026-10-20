"""Prove live per-user rate limiting, audit evidence, metrics and recovery."""

import asyncio
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "deploy" / "secrets" / "local.secrets.yaml"
APP = "http://app.127-0-0-1.nip.io"
MCP_URL = "http://mcp.127-0-0-1.nip.io/mcp"
XRW = {"X-Requested-With": "phase-10.7-gate"}


class GateError(RuntimeError):
    pass


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}".rstrip(), flush=True)
    if not condition:
        raise GateError(f"{label}: {detail}")


def secret_value(text: str, key: str) -> str:
    match = re.search(rf'^\s*{key}:\s*"?([^"\r\n]+)', text, re.MULTILINE)
    if not match:
        raise GateError(f"{key} is missing from {SECRETS}")
    return match.group(1).strip()


def gateway_metrics() -> str:
    code = (
        "import urllib.request; "
        "print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"
    )
    result = subprocess.run(
        [
            "kubectl", "-n", "platform", "exec", "deploy/gateway", "-c", "gateway",
            "--", "python", "-c", code,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise GateError(f"gateway metrics read failed: {result.stderr.strip()}")
    return result.stdout


@asynccontextmanager
async def mcp_session(key: str):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as transport,
        Client(streamable_http_client(MCP_URL, http_client=transport)) as client,
    ):
        yield client


async def main() -> None:
    secret_text = SECRETS.read_text(encoding="utf-8")
    key_id = ""
    async with httpx2.AsyncClient(base_url=APP, timeout=30) as admin:
        login = await admin.post(
            "/api/auth/login",
            json={
                "email": secret_value(secret_text, "bootstrapAdminEmail"),
                "password": secret_value(secret_text, "bootstrapAdminPassword"),
            },
        )
        check("admin login", login.status_code == 200, f"HTTP {login.status_code}")
        created = await admin.post("/api/keys", json={"name": "phase-10.7-gate"}, headers=XRW)
        check("temporary key created", created.status_code == 201, f"HTTP {created.status_code}")
        key_id, key = created.json()["id"], created.json()["key"]
        try:
            successes = 0
            limited = 0
            async with mcp_session(key) as client:
                for _ in range(25):
                    result = await client.call_tool("woo__get_store_info", {})
                    if result.is_error and "Rate limit reached" in result.content[0].text:
                        limited += 1
                    elif not result.is_error:
                        successes += 1
                    else:
                        raise GateError(f"unexpected tool result: {result.content[0].text}")
                check(
                    "burst permits capacity and limits excess calls",
                    successes >= 20 and limited >= 1 and successes + limited == 25,
                    f"success={successes}, limited={limited}",
                )

                deadline = time.monotonic() + 10
                while True:
                    recovered = await client.call_tool("woo__get_store_info", {})
                    if not recovered.is_error:
                        break
                    if time.monotonic() >= deadline:
                        raise GateError("rate-limit bucket did not recover within 10 seconds")
                    await asyncio.sleep(1)
                check("token bucket refills", True)

            audit_response = await admin.get(
                "/api/audit",
                params={"tool": "woo__get_store_info", "outcome": "rate_limited", "source": "mcp", "limit": 20},
            )
            check("rate-limit audit query succeeds", audit_response.status_code == 200)
            rows = audit_response.json()["items"]
            check(
                "limited calls audit the real tool",
                len(rows) >= limited and all(row["server_slug"] == "woo" for row in rows[:limited]),
                f"rows={len(rows)}",
            )

            metrics = gateway_metrics()
            check(
                "tool-call rate-limit metric increments",
                'urumi_rate_limited_total{scope="tool_call"}' in metrics,
            )
        finally:
            if key_id:
                revoked = await admin.delete(f"/api/keys/{key_id}", headers=XRW)
                check("temporary key revoked", revoked.status_code == 204, f"HTTP {revoked.status_code}")

    print("Phase 10.7 live rate-limit gate passed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
