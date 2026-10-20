"""Prove the compact MCP search surface, permissions, delegation and live chat auto mode."""

import asyncio
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "deploy" / "secrets" / "local.secrets.yaml"
DEMO_USERS = ROOT / "deploy" / "secrets" / "local.demo-users.yaml"
APP = "http://app.127-0-0-1.nip.io"
SEARCH_URL = "http://mcp.127-0-0-1.nip.io/mcp/search"
XRW = {"X-Requested-With": "phase-10.6-gate"}
LIVE_CHAT = "--live-chat" in sys.argv[1:]


class GateError(RuntimeError):
    pass


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}".rstrip(), flush=True)
    if not condition:
        raise GateError(f"{label}: {detail}")


def yaml_value(text: str, key: str, path: Path) -> str:
    match = re.search(rf'^\s*{re.escape(key)}:\s*"?([^"\r\n]+)', text, re.MULTILINE)
    if not match:
        raise GateError(f"{key} is missing from {path}")
    return match.group(1).strip()


def helm_threshold(value: int) -> None:
    args = [
        "helm", "upgrade", "--install", "platform", "deploy/charts/platform", "-n", "platform",
        "-f", "deploy/values/platform/values-local.yaml", "-f", "deploy/secrets/local.secrets.yaml",
        "--set", f"chat.maxDirectTools={value}", "--wait", "--timeout", "3m",
    ]
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=210, check=False)
    if result.returncode:
        raise GateError(f"Helm threshold {value} failed: {(result.stderr or result.stdout)[-800:]}")


@asynccontextmanager
async def search_session(key: str):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as transport,
        Client(streamable_http_client(SEARCH_URL, http_client=transport)) as client,
    ):
        yield client


async def new_key(http: httpx2.AsyncClient, name: str) -> tuple[str, str]:
    response = await http.post("/api/keys", json={"name": name}, headers=XRW)
    check(f"{name} key created", response.status_code == 201, f"HTTP {response.status_code}")
    return response.json()["id"], response.json()["key"]


async def main() -> None:
    secrets_text = SECRETS.read_text(encoding="utf-8")
    demo_text = DEMO_USERS.read_text(encoding="utf-8")
    admin_key_id = member_key_id = ""
    threshold_changed = False
    async with (
        httpx2.AsyncClient(base_url=APP, timeout=120) as admin,
        httpx2.AsyncClient(base_url=APP, timeout=120) as member,
    ):
        admin_login = await admin.post(
            "/api/auth/login",
            json={
                "email": yaml_value(secrets_text, "bootstrapAdminEmail", SECRETS),
                "password": yaml_value(secrets_text, "bootstrapAdminPassword", SECRETS),
            },
        )
        member_login = await member.post(
            "/api/auth/login",
            json={
                "email": "bob@urumi.test",
                "password": yaml_value(demo_text, "bob@urumi.test", DEMO_USERS),
            },
        )
        check("admin login", admin_login.status_code == 200, f"HTTP {admin_login.status_code}")
        check("member login after credential rotation", member_login.status_code == 200, f"HTTP {member_login.status_code}")
        admin_key_id, admin_key = await new_key(admin, "phase-10.6-admin")
        member_key_id, member_key = await new_key(member, "phase-10.6-member")
        try:
            async with search_session(admin_key) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                check("search endpoint lists exactly two meta-tools", names == {
                    "gateway__search_tools", "gateway__call_tool"
                }, str(sorted(names)))
                found = await client.call_tool("gateway__search_tools", {"query": "orders today", "limit": 5})
                matches = found.structured_content["tools"]
                selected = next((tool["name"] for tool in matches if tool["name"] == "woo__list_orders"), "")
                check("BM25 finds the order tool", selected == "woo__list_orders")
                called = await client.call_tool("gateway__call_tool", {"name": selected, "arguments": {"period": "today"}})
                check("delegated real tool succeeds", not called.is_error)

            async with search_session(member_key) as client:
                found = await client.call_tool("gateway__search_tools", {"query": "create product", "limit": 20})
                names = {tool["name"] for tool in found.structured_content["tools"]}
                check("search hides member-forbidden writes", "woo__create_product" not in names)
                denied = await client.call_tool(
                    "gateway__call_tool", {"name": "woo__create_product", "arguments": {"name": "Denied"}}
                )
                check("direct meta-call still enforces role", denied.is_error and "not allowed" in denied.content[0].text)

            audit = (await admin.get(
                "/api/audit", params={"tool": "woo__list_orders", "source": "mcp", "limit": 20}
            )).json()["items"]
            delegated = next((row for row in audit if (row.get("args_preview") or {}).get("via") == "search"), None)
            check("audit records the real delegated tool", bool(delegated) and delegated["server_slug"] == "woo")

            if LIVE_CHAT:
                helm_threshold(1)
                threshold_changed = True
                chat = await admin.post(
                    "/api/chat",
                    json={"messages": [{
                        "role": "user",
                        "content": "Use tool search to find and call the WooCommerce store-info tool, then tell me its timezone.",
                    }]},
                    headers=XRW,
                )
                body = chat.json()
                calls = [call["name"] for call in body.get("tool_calls", [])]
                check(
                    "live chat answers in automatic search mode",
                    chat.status_code == 200 and bool(body.get("answer")),
                    str(calls),
                )
                check(
                    "chat visibly searches then delegates",
                    "gateway__search_tools" in calls and "gateway__call_tool" in calls,
                    str(calls),
                )
        finally:
            cleanup_errors: list[str] = []
            if threshold_changed:
                try:
                    helm_threshold(20)
                    check("chat threshold restored", True, "20")
                except GateError as exc:
                    cleanup_errors.append(str(exc))
            if member_key_id:
                revoked = await member.delete(f"/api/keys/{member_key_id}", headers=XRW)
                if revoked.status_code == 204:
                    check("member key revoked", True)
                else:
                    cleanup_errors.append(f"member key revoke HTTP {revoked.status_code}")
            if admin_key_id:
                revoked = await admin.delete(f"/api/keys/{admin_key_id}", headers=XRW)
                if revoked.status_code == 204:
                    check("admin key revoked", True)
                else:
                    cleanup_errors.append(f"admin key revoke HTTP {revoked.status_code}")
            if cleanup_errors:
                raise GateError("; ".join(cleanup_errors))

    suffix = " including external live chat" if LIVE_CHAT else " without external LLM egress"
    print(f"Phase 10.6 many-tools gate passed{suffix}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
