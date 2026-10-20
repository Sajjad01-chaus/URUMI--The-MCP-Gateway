"""Prove live metrics, audit-backed stats, failure classification and UI delivery."""

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
XRW = {"X-Requested-With": "phase-10.4-gate"}


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


def kubectl(*args: str, timeout: int = 180) -> str:
    result = subprocess.run(["kubectl", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise GateError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def internal_metrics(deployment: str, container: str, port: int) -> str:
    code = f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{port}/metrics').read().decode())"
    return kubectl("-n", "platform", "exec", f"deploy/{deployment}", "-c", container, "--", "python", "-c", code)


@asynccontextmanager
async def mcp_session(key: str):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as transport,
        Client(streamable_http_client(MCP_URL, http_client=transport)) as client,
    ):
        yield client


async def wait_status(http: httpx2.AsyncClient, slug: str, expected: str, seconds: int = 75) -> dict:
    deadline = time.monotonic() + seconds
    last = "missing"
    while time.monotonic() < deadline:
        rows = (await http.get("/api/servers")).json()
        row = next((item for item in rows if item["slug"] == slug), None)
        if row:
            last = f"{row['status']}: {row.get('last_error')}"
            if row["status"] == expected:
                return row
        await asyncio.sleep(2)
    raise GateError(f"{slug} did not reach {expected}; last={last}")


async def main() -> None:
    secret_text = SECRETS.read_text(encoding="utf-8")
    key_id = ""
    notes_scaled_down = False
    async with httpx2.AsyncClient(base_url=APP, timeout=30) as admin:
        login = await admin.post(
            "/api/auth/login",
            json={
                "email": secret_value(secret_text, "bootstrapAdminEmail"),
                "password": secret_value(secret_text, "bootstrapAdminPassword"),
            },
        )
        check("admin login", login.status_code == 200, f"HTTP {login.status_code}")
        created = await admin.post("/api/keys", json={"name": "phase-10.4-gate"}, headers=XRW)
        check("temporary key created", created.status_code == 201, f"HTTP {created.status_code}")
        key_id, key = created.json()["id"], created.json()["key"]
        try:
            async with mcp_session(key) as client:
                result = await client.call_tool("woo__get_store_info", {})
                check("tool call succeeds", not result.is_error)

            stats = (await admin.get("/api/stats", params={"window": "15m"})).json()
            woo = next(row for row in stats["servers"] if row["slug"] == "woo")
            check("stats include the live tool call", woo["calls"] > 0 and woo["p95_ms"] >= woo["p50_ms"])
            check("stats expose per-user sources", any(row["mcp"] > 0 or row["chat"] > 0 for row in stats["users"]))

            gateway_metrics = internal_metrics("gateway", "gateway", 8000)
            for metric in (
                "urumi_tool_calls_total",
                "urumi_tool_call_duration_seconds",
                "urumi_upstream_up",
                "urumi_upstream_health_checks_total",
                "urumi_mcp_sessions",
                "urumi_rate_limited_total",
            ):
                check(f"gateway exports {metric}", metric in gateway_metrics)
            provisioner_metrics = internal_metrics("provisioner", "provisioner", 9100)
            check("provisioner exports operation counter", "urumi_provisioning_total" in provisioner_metrics)
            check("provisioner exports duration histogram", "urumi_provisioning_duration_seconds" in provisioner_metrics)

            public_metrics = await admin.get("/metrics")
            check("gateway metrics are not routed publicly", "urumi_tool_calls_total" not in public_metrics.text)

            html = (await admin.get("/stats")).text
            asset = re.search(r'src="([^"]+\.js)"', html)
            bundle = (await admin.get(asset.group(1))).text if asset else ""
            check("Stats page is in the deployed dashboard", "Audit-backed traffic and latency" in bundle)

            kubectl("-n", "platform", "scale", "deploy/notes-mcp", "--replicas=0")
            notes_scaled_down = True
            failed = await wait_status(admin, "notes", "unreachable")
            check("dead upstream has a classified reason", "connection refused" in (failed["last_error"] or ""))
            failed_stats = (await admin.get("/api/stats", params={"window": "15m"})).json()
            notes = next(row for row in failed_stats["servers"] if row["slug"] == "notes")
            check("Stats turns the dead upstream red", notes["status"] == "unreachable" and notes["last_error"])
            gateway_metrics = internal_metrics("gateway", "gateway", 8000)
            check("failure class is a bounded metric label", 'server="notes"' in gateway_metrics and 'result="refused"' in gateway_metrics)
        finally:
            if notes_scaled_down:
                kubectl("-n", "platform", "scale", "deploy/notes-mcp", "--replicas=1")
                kubectl("-n", "platform", "rollout", "status", "deploy/notes-mcp", "--timeout=120s", timeout=130)
                recovered = await wait_status(admin, "notes", "connected")
                check("notes recovers", recovered["status"] == "connected")
            if key_id:
                revoked = await admin.delete(f"/api/keys/{key_id}", headers=XRW)
                check("temporary key revoked", revoked.status_code == 204, f"HTTP {revoked.status_code}")

    print("Phase 10.4 live gate passed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
