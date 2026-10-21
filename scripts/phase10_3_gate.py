"""Prove hosted MCP deployment, isolation, root rejection and teardown."""

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
ENV = os.environ.get("URUMI_ENV", "local")
SECRETS = ROOT / "deploy" / "secrets" / f"{ENV}.secrets.yaml"
DOMAIN = sys.argv[1] if len(sys.argv) > 1 else "127-0-0-1.nip.io"
SCHEME = "https" if ENV == "prod" else "http"
APP = f"{SCHEME}://app.{DOMAIN}"
MCP_URL = f"{SCHEME}://mcp.{DOMAIN}/mcp"
HOSTED_FX_IMAGE = os.environ.get("HOSTED_FX_IMAGE", "urumi/fx-mcp:dev")
SKIP_ROOT_FIXTURE = os.environ.get("GATE_SKIP_ROOT_FIXTURE") == "1"
XRW = {"X-Requested-With": "phase-10.3-gate"}
SLUGS = {"fx2", "root-fixture"}


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


def command(args: list[str], timeout: int = 180, check_result: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if check_result and result.returncode:
        raise GateError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result


def kubectl(*args: str, timeout: int = 180, check_result: bool = True) -> subprocess.CompletedProcess[str]:
    return command(["kubectl", *args], timeout=timeout, check_result=check_result)


@asynccontextmanager
async def mcp_session(key: str):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as transport,
        Client(streamable_http_client(MCP_URL, http_client=transport)) as client,
    ):
        yield client


async def names(client: Client) -> set[str]:
    return {tool.name for tool in (await client.list_tools()).tools}


async def list_hosted(http: httpx2.AsyncClient, deleted: bool = False) -> list[dict]:
    response = await http.get("/api/hosted-servers", params={"include": "deleted"} if deleted else None)
    response.raise_for_status()
    return response.json()


async def wait_release(http: httpx2.AsyncClient, release_id: str, expected: str, seconds: int) -> dict:
    deadline = time.monotonic() + seconds
    last = "missing"
    while time.monotonic() < deadline:
        row = next(
            (item for item in await list_hosted(http, expected == "deleted") if item["id"] == release_id),
            None,
        )
        if row:
            last = f"{row['status']}: {row['reason']}"
            if row["status"] == expected:
                return row
            if expected == "ready" and row["status"] == "failed":
                raise GateError(last)
        await asyncio.sleep(3)
    raise GateError(f"timed out waiting for {expected}; last state: {last}")


async def delete_release(http: httpx2.AsyncClient, row: dict) -> None:
    if row["status"] != "deleting":
        response = await http.delete(f"/api/hosted-servers/{row['id']}", headers=XRW)
        check(f"{row['slug']} delete accepted", response.status_code == 202, f"HTTP {response.status_code}")
    await wait_release(http, row["id"], "deleted", 180)


async def clean_previous(http: httpx2.AsyncClient) -> None:
    rows = [row for row in await list_hosted(http) if row["slug"] in SLUGS]
    for row in rows:
        await delete_release(http, row)


def verify_hardening() -> None:
    namespace = "mcp-fx2"
    label = kubectl("get", "namespace", namespace, "-o", "jsonpath={.metadata.labels.urumi\\.io/mcp-upstreams}").stdout
    check("hosted namespace enables gateway egress", label == "true", label)
    deployment = json.loads(kubectl("-n", namespace, "get", "deployment", "mcp", "-o", "json").stdout)
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    security = container["securityContext"]
    check("hosted pod runs as non-root", pod["securityContext"]["runAsNonRoot"] is True)
    check("hosted pod has no service-account token", pod["automountServiceAccountToken"] is False)
    check("hosted root filesystem is read-only", security["readOnlyRootFilesystem"] is True)
    check("hosted privilege escalation is disabled", security["allowPrivilegeEscalation"] is False)
    check("hosted container drops every capability", security["capabilities"]["drop"] == ["ALL"])
    check("hosted Secret exists", "secret/mcp-env" in kubectl("-n", namespace, "get", "secret", "-o", "name").stdout)
    can_read = command(
        [
            "kubectl", "auth", "can-i", "get", "secrets", "-n", namespace,
            "--as=system:serviceaccount:mcp-fx2:mcp",
        ],
        check_result=False,
    )
    check("hosted service account has no Secret RBAC", can_read.stdout.strip() == "no", can_read.stdout.strip())


def verify_isolation() -> None:
    denied = kubectl(
        "-n", "mcp-fx2", "exec", "deployment/mcp", "--", "python", "-c",
        "import socket; socket.create_connection(('postgres.platform.svc.cluster.local',5432),2)",
        check_result=False,
    )
    check("hosted MCP cannot reach platform Postgres", denied.returncode != 0)
    pod = "phase103-random"
    kubectl("-n", "platform", "delete", "pod", pod, "--ignore-not-found=true", "--wait=true")
    try:
        kubectl(
            "-n", "platform", "run", pod, "--image=busybox:1.37",
            "--labels=urumi.io/netpol-test=true", "--restart=Never", "--command", "--", "sleep", "120",
        )
        kubectl("-n", "platform", "wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=90s")
        denied = kubectl(
            "-n", "platform", "exec", pod, "--", "sh", "-c", "nc -w 3 mcp.mcp-fx2 8000 </dev/null",
            check_result=False,
        )
        check("random platform pod cannot reach hosted MCP", denied.returncode != 0)
    finally:
        kubectl("-n", "platform", "delete", "pod", pod, "--ignore-not-found=true", "--wait=false", check_result=False)


async def run() -> None:
    secret_text = SECRETS.read_text(encoding="utf-8")
    key_id = ""
    created: list[dict] = []
    async with httpx2.AsyncClient(base_url=APP, timeout=60) as http:
        login = await http.post(
            "/api/auth/login",
            json={
                "email": secret_value(secret_text, "bootstrapAdminEmail"),
                "password": secret_value(secret_text, "bootstrapAdminPassword"),
            },
        )
        check("admin login", login.status_code == 200, f"HTTP {login.status_code}")
        await clean_previous(http)
        key_response = await http.post("/api/keys", json={"name": "phase-10.3-gate"}, headers=XRW)
        check("temporary MCP key created", key_response.status_code == 201, f"HTTP {key_response.status_code}")
        key, key_id = key_response.json()["key"], key_response.json()["id"]
        try:
            async with mcp_session(key) as client:
                baseline = await names(client)
                token = secrets.token_urlsafe(24)
                payload = {
                    "name": "Hosted FX",
                    "slug": "fx2",
                    "image": HOSTED_FX_IMAGE,
                    "port": 8000,
                    "path": "/mcp",
                    "env": {"ALLOWED_HOSTS": "mcp.mcp-fx2.svc.cluster.local:*"},
                    "secret_env": {"FX_API_KEY": token},
                    "auth_type": "header",
                    "auth_header_name": "X-API-Key",
                    "credential_env": "FX_API_KEY",
                    "internet_egress": False,
                    "size": "small",
                }
                response = await http.post("/api/hosted-servers", json=payload, headers=XRW)
                check("hosted FX create accepted", response.status_code == 201, f"HTTP {response.status_code}")
                fx2 = response.json()
                created.append(fx2)
                check("secret value is write-only", token not in response.text)
                fx2 = await wait_release(http, fx2["id"], "ready", 180)
                deadline = time.monotonic() + 30
                tool_names = await names(client)
                while not any(name.startswith("fx2__") for name in tool_names) and time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    tool_names = await names(client)
                check("hosted FX tools registered", "fx2__convert_currency" in tool_names)
                result = await client.call_tool(
                    "fx2__convert_currency",
                    {"amount": 10, "from_currency": "USD", "to_currency": "EUR"},
                )
                check("hosted FX tool call succeeds", not result.is_error)
                verify_hardening()
                verify_isolation()

                root = None
                if not SKIP_ROOT_FIXTURE:
                    root_payload = {
                        "name": "Root Fixture",
                        "slug": "root-fixture",
                        "image": "docker.io/library/busybox:1.37",
                        "port": 8000,
                        "path": "/mcp",
                        "env": {},
                        "secret_env": {},
                        "auth_type": "none",
                        "internet_egress": False,
                        "size": "small",
                    }
                    response = await http.post("/api/hosted-servers", json=root_payload, headers=XRW)
                    check("root fixture create accepted", response.status_code == 201, f"HTTP {response.status_code}")
                    root = response.json()
                    created.append(root)
                    root = await wait_release(http, root["id"], "failed", 150)
                    check("root image rejected clearly", "must run as non-root" in root["reason"], root["reason"])

                await delete_release(http, fx2)
                check("hosted tools removed immediately", not any(name.startswith("fx2__") for name in await names(client)))
                check("hosted namespace removed", kubectl("get", "namespace", "mcp-fx2", check_result=False).returncode != 0)
                check(
                    "hosted Helm release removed",
                    command(["helm", "status", "mcp-fx2", "-n", "mcp-fx2"], check_result=False).returncode != 0,
                )
                if root is not None:
                    await delete_release(http, root)
                    check(
                        "root fixture namespace removed",
                        kubectl("get", "namespace", "mcp-root-fixture", check_result=False).returncode != 0,
                    )
                check("tool catalog returned to baseline", await names(client) == baseline)
        finally:
            for row in created:
                current = next((item for item in await list_hosted(http) if item["id"] == row["id"]), None)
                if current:
                    try:
                        await delete_release(http, current)
                    except Exception as exc:
                        print(f"cleanup warning for {row['slug']}: {exc}", flush=True)
            if key_id:
                await http.delete(f"/api/keys/{key_id}", headers=XRW)


if __name__ == "__main__":
    asyncio.run(run())
