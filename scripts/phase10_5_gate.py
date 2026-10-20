"""Demonstrate zero-failure upgrade, automatic rollback and manual rollback locally."""

import asyncio
import json
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
XRW = {"X-Requested-With": "phase-10.5-gate"}
TAG_A = "phase10.5-a"
TAG_B = "phase10.5-b"


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


def command(args: list[str], timeout: int = 300, expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != expected:
        raise GateError(f"{' '.join(args)} exited {result.returncode}: {(result.stderr or result.stdout)[-800:]}")
    return result


def helm_upgrade_args(image: str) -> list[str]:
    return [
        "helm", "upgrade", "--install", "platform", "deploy/charts/platform", "-n", "platform",
        "-f", "deploy/values/platform/values-local.yaml", "-f", "deploy/secrets/local.secrets.yaml",
        "--set-string", f"gateway.image=urumi/gateway:{image}", "--wait", "--timeout", "3m",
    ]


def current_revision() -> int:
    history = json.loads(command(["helm", "history", "platform", "-n", "platform", "-o", "json"]).stdout)
    return int(history[-1]["revision"])


@asynccontextmanager
async def mcp_session(key: str):
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=30) as transport,
        Client(streamable_http_client(MCP_URL, http_client=transport)) as client,
    ):
        yield client


async def call_once(key: str, tool: str) -> None:
    async with mcp_session(key) as client:
        result = await client.call_tool(tool, {})
        if result.is_error:
            raise GateError(result.content[0].text)


async def run_with_calls(args: list[str], key: str, tool: str, expect_failure: bool = False) -> tuple[int, int, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    successes = 0
    failures: list[str] = []
    while process.returncode is None:
        try:
            await call_once(key, tool)
            successes += 1
        except Exception as exc:
            failures.append(str(exc))
        await asyncio.sleep(0.5)
    output = (await process.stdout.read()).decode(errors="replace")
    check("upgrade command outcome", (process.returncode != 0) if expect_failure else (process.returncode == 0), f"exit {process.returncode}")
    check("calls stayed available", not failures and successes > 0, f"{successes} ok, {len(failures)} failed")
    return process.returncode or 0, successes, output


async def wait_version(http: httpx2.AsyncClient, expected: str, seconds: int = 90) -> None:
    deadline = time.monotonic() + seconds
    last = "missing"
    while time.monotonic() < deadline:
        response = await http.get("/api/meta")
        if response.status_code == 200:
            last = response.json()["gateway_version"]
            if last == expected:
                return
        await asyncio.sleep(1)
    raise GateError(f"gateway version did not become {expected}; last={last}")


async def main() -> None:
    for tag in (TAG_A, TAG_B):
        command(["docker", "tag", "urumi/gateway:dev", f"urumi/gateway:{tag}"])
        command(["k3d", "image", "import", "-c", "urumi", f"urumi/gateway:{tag}"], timeout=180)

    secrets = SECRETS.read_text(encoding="utf-8")
    key_id = ""
    async with httpx2.AsyncClient(base_url=APP, timeout=30) as admin:
        login = await admin.post(
            "/api/auth/login",
            json={
                "email": secret_value(secrets, "bootstrapAdminEmail"),
                "password": secret_value(secrets, "bootstrapAdminPassword"),
            },
        )
        check("admin login", login.status_code == 200, f"HTTP {login.status_code}")
        created = await admin.post("/api/keys", json={"name": "phase-10.5-gate"}, headers=XRW)
        check("temporary key created", created.status_code == 201, f"HTTP {created.status_code}")
        key_id, key = created.json()["id"], created.json()["key"]
        try:
            command(helm_upgrade_args(TAG_A))
            await wait_version(admin, TAG_A)
            baseline_revision = current_revision()
            check("baseline version visible", True, f"revision {baseline_revision}, {TAG_A}")

            _, good_calls, _ = await run_with_calls(helm_upgrade_args(TAG_B), key, "woo__get_store_info")
            await wait_version(admin, TAG_B)
            good_revision = current_revision()
            check("good rolling upgrade", good_revision > baseline_revision, f"revision {good_revision}, {good_calls} calls")

            bad = helm_upgrade_args(TAG_B)
            bad[bad.index("--wait"):bad.index("--timeout") + 2] = ["--rollback-on-failure", "--timeout", "45s"]
            bad.extend(["--set-string", "notesMcp.image=urumi/notes-mcp:missing-phase10-5"])
            _, bad_calls, _ = await run_with_calls(bad, key, "notes__list_notes", expect_failure=True)
            await wait_version(admin, TAG_B)
            notes_image = command([
                "kubectl", "-n", "platform", "get", "deploy/notes-mcp",
                "-o", "jsonpath={.spec.template.spec.containers[0].image}",
            ]).stdout
            check("failed upgrade rolled back", notes_image == "urumi/notes-mcp:dev", f"{notes_image}, {bad_calls} calls")
            check("rollback is recorded in Helm history", current_revision() > good_revision)

            rollback = ["helm", "rollback", "platform", str(baseline_revision), "-n", "platform", "--wait", "--timeout", "3m"]
            _, rollback_calls, _ = await run_with_calls(rollback, key, "woo__get_store_info")
            await wait_version(admin, TAG_A)
            check("manual rollback restored prior version", True, f"{TAG_A}, {rollback_calls} calls")

            restore = ["helm", "rollback", "platform", str(good_revision), "-n", "platform", "--wait", "--timeout", "3m"]
            command(restore)
            await wait_version(admin, TAG_B)
            check("final desired version restored", True, TAG_B)
        finally:
            if key_id:
                revoked = await admin.delete(f"/api/keys/{key_id}", headers=XRW)
                check("temporary key revoked", revoked.status_code == 204, f"HTTP {revoked.status_code}")

    print("Phase 10.5 upgrade/rollback gate passed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
