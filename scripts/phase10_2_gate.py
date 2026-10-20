"""Prove concurrent multi-store provisioning, routing, isolation and teardown."""

import asyncio
import json
import logging
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
DOMAIN = "127-0-0-1.nip.io"
XRW = {"X-Requested-With": "phase-10.2-gate"}
STORES = (("north", "North Gate Store"), ("south", "South Gate Store"))
log = logging.getLogger(__name__)


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
    result = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
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


async def tool_names(client: Client) -> set[str]:
    return {tool.name for tool in (await client.list_tools()).tools}


async def wait_store(http: httpx2.AsyncClient, store_id: str, expected: str, seconds: int) -> dict:
    deadline = time.monotonic() + seconds
    last = "missing"
    while time.monotonic() < deadline:
        params = {"include": "deleted"} if expected == "deleted" else None
        response = await http.get("/api/stores", params=params)
        response.raise_for_status()
        store = next((item for item in response.json() if item["id"] == store_id), None)
        if store:
            last = f"{store['status']}: {store['reason']}"
            if store["status"] == expected:
                return store
            if expected == "ready" and store["status"] == "failed":
                raise GateError(last)
        await asyncio.sleep(5)
    raise GateError(f"timed out waiting for {expected}; last state: {last}")


async def wait_tools(http: httpx2.AsyncClient, prefixes: set[str], seconds: int = 30) -> set[str]:
    deadline = time.monotonic() + seconds
    names: set[str] = set()
    while time.monotonic() < deadline:
        response = await http.get("/api/tools")
        response.raise_for_status()
        names = {item["exposed_name"] for item in response.json()}
        if all(any(name.startswith(f"{prefix}__") for name in names) for prefix in prefixes):
            return names
        await asyncio.sleep(2)
    raise GateError(f"tool prefixes did not appear: {sorted(prefixes)}")


async def remove_existing(http: httpx2.AsyncClient) -> None:
    active = (await http.get("/api/stores")).json()
    rows = [row for row in active if row["slug"] in {slug for slug, _ in STORES} and not row["built_in"]]
    for row in rows:
        if row["status"] != "deleting":
            response = await http.delete(f"/api/stores/{row['id']}", headers=XRW)
            check(f"old {row['slug']} cleanup accepted", response.status_code == 202, f"HTTP {response.status_code}")
    await asyncio.gather(*(wait_store(http, row["id"], "deleted", 360) for row in rows))


def verify_tenant_objects(slug: str) -> None:
    namespace = f"store-{slug}"
    label = kubectl("get", "namespace", namespace, "-o", "jsonpath={.metadata.labels.urumi\\.io/mcp-upstreams}").stdout
    check(f"{slug} namespace enables MCP egress", label == "true", label)
    check(f"{slug} PVC exists", bool(kubectl("-n", namespace, "get", "pvc", "-o", "name").stdout.strip()))
    check(f"{slug} Secret exists", bool(kubectl("-n", namespace, "get", "secret", "-o", "name").stdout.strip()))


def verify_cross_tenant_isolation() -> None:
    source = "store-north"
    pod = "phase102-cross"
    labels = "urumi.io/netpol-test=true"
    kubectl("-n", source, "delete", "pod", pod, "--ignore-not-found=true", "--wait=true")
    try:
        kubectl(
            "-n", source, "run", pod, "--image=busybox:1.37", f"--labels={labels}",
            "--restart=Never", "--command", "--", "sleep", "300",
        )
        kubectl("-n", source, "wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=120s")
        for service, port in (("woo-mcp.store-south", "8000"), ("mysql.store-south", "3306"), ("wordpress.store-south", "80")):
            result = kubectl(
                "-n", source, "exec", pod, "--", "sh", "-c", f"nc -w 3 {service} {port} </dev/null",
                check_result=False,
            )
            check(f"cross-tenant {service}:{port} denied", result.returncode != 0)
    finally:
        kubectl("-n", source, "delete", "pod", pod, "--ignore-not-found=true", "--wait=false", check_result=False)


async def delete_and_verify(
    http: httpx2.AsyncClient, client: Client, store: dict, expect_notification: bool
) -> None:
    slug = store["slug"]
    namespace = f"store-{slug}"
    notification = None
    if expect_notification:
        subscription_context = client.listen(tools_list_changed=True)
        subscription = await subscription_context.__aenter__()
        notification = asyncio.create_task(anext(aiter(subscription)))
    else:
        subscription_context = None
    try:
        response = await http.delete(f"/api/stores/{store['id']}", headers=XRW)
        check(f"{slug} delete accepted", response.status_code == 202, f"HTTP {response.status_code}")
        servers = (await http.get("/api/servers")).json()
        check(f"{slug} upstream removed before teardown", not any(item["slug"] == slug for item in servers))
        if notification is not None:
            await asyncio.wait_for(notification, timeout=10)
            check(f"{slug} deletion pushed tools/list_changed", True)
        names = await tool_names(client)
        check(f"{slug} tools disappeared", not any(name.startswith(f"{slug}__") for name in names))
        await wait_store(http, store["id"], "deleted", 360)
        check(f"{slug} namespace removed", kubectl("get", "namespace", namespace, check_result=False).returncode != 0)
        helm = command(["helm", "status", f"store-{slug}", "-n", namespace], check_result=False)
        check(f"{slug} Helm release removed", helm.returncode != 0)
        managed_slugs = kubectl(
            "-n", "platform", "exec", "statefulset/postgres", "--", "psql", "-U", "gateway", "-d", "gateway",
            "-tAc", "SELECT slug FROM upstream_servers WHERE is_managed ORDER BY slug",
        ).stdout.split()
        check(f"{slug} upstream database row removed", slug not in managed_slugs, str(managed_slugs))
    finally:
        if notification is not None and not notification.done():
            notification.cancel()
        if subscription_context is not None:
            await subscription_context.__aexit__(None, None, None)


async def run() -> None:
    text = SECRETS.read_text(encoding="utf-8")
    created: list[dict] = []
    key_id = ""
    async with httpx2.AsyncClient(base_url=APP, timeout=180) as http:
        login = await http.post(
            "/api/auth/login",
            json={
                "email": secret_value(text, "bootstrapAdminEmail"),
                "password": secret_value(text, "bootstrapAdminPassword"),
            },
        )
        check("admin login", login.status_code == 200, f"HTTP {login.status_code}")
        await remove_existing(http)
        active = [row for row in (await http.get("/api/stores")).json() if not row["built_in"]]
        check("store quota starts empty", not active, json.dumps([(row["slug"], row["status"]) for row in active]))

        key_response = await http.post("/api/keys", json={"name": "phase-10.2-gate"}, headers=XRW)
        check("temporary MCP key created", key_response.status_code == 201, f"HTTP {key_response.status_code}")
        key, key_id = key_response.json()["key"], key_response.json()["id"]

        try:
            async with mcp_session(key) as client:
                baseline = await tool_names(client)
                async with client.listen(tools_list_changed=True) as subscription:
                    notification = asyncio.create_task(anext(aiter(subscription)))
                    started = time.monotonic()
                    responses = await asyncio.gather(*(
                        http.post("/api/stores", json={"name": name, "slug": slug}, headers=XRW)
                        for slug, name in STORES
                    ))
                    check("two concurrent creates accepted", all(r.status_code == 201 for r in responses), str([r.status_code for r in responses]))
                    created = [response.json() for response in responses]
                    ready = await asyncio.gather(*(wait_store(http, row["id"], "ready", 360) for row in created))
                    elapsed = time.monotonic() - started
                    check("both stores Ready under six minutes", elapsed < 360, f"{elapsed:.1f}s")
                    await wait_tools(http, {"north", "south"}, 30)
                    await asyncio.wait_for(notification, timeout=10)
                    check("creation pushed tools/list_changed", True)

                names = await tool_names(client)
                async with httpx2.AsyncClient(follow_redirects=True, timeout=30) as public:
                    for slug, _ in STORES:
                        check(f"{slug} tool prefix visible", any(name.startswith(f"{slug}__") for name in names))
                        storefront = await public.get(f"http://store-{slug}.{DOMAIN}")
                        check(f"{slug} storefront responds", storefront.status_code == 200)
                        verify_tenant_objects(slug)
                        info = await client.call_tool(f"{slug}__get_store_info", {})
                        check(f"{slug} routed tool succeeds", not info.is_error)
                check("v1 tool catalog remains visible", {"woo__get_store_info", "notes__search", "fx__convert_currency"} <= names)

                netpol = await asyncio.to_thread(
                    command,
                    [
                        "bash", "scripts/netpol-test.sh", DOMAIN,
                        "--tenant", "store-north",
                        "--kubeconfig", "/mnt/c/Users/chaus/.kube/config",
                    ],
                    300,
                )
                print(netpol.stdout, end="")
                check(
                    "v1 plus tenant NetworkPolicy matrix",
                    "33 passed, 0 failed" in netpol.stdout and netpol.returncode == 0,
                )
                verify_cross_tenant_isolation()

                chat = await http.post(
                    "/api/chat",
                    json={"messages": [{
                        "role": "user",
                        "content": "Call north__get_store_info and south__get_store_info, then compare their store URLs in one sentence.",
                    }]},
                    headers=XRW,
                    timeout=180,
                )
                check("cross-store chat answered", chat.status_code == 200, f"HTTP {chat.status_code}")
                body = chat.json()
                calls = {item["name"] for item in body["tool_calls"] if not item["is_error"]}
                check("chat called both stores", {"north__get_store_info", "south__get_store_info"} <= calls, str(sorted(calls)))
                for slug, _ in STORES:
                    audit = await http.get("/api/audit", params={"server": slug, "outcome": "success", "limit": 20})
                    rows = audit.json()["items"]
                    check(f"{slug} audit rows recorded", any(row["server_slug"] == slug for row in rows))

                await delete_and_verify(http, client, ready[0], expect_notification=True)
                names = await tool_names(client)
                check("south store remains routable", "south__get_store_info" in names)
                result = await client.call_tool("south__get_store_info", {})
                check("south call still succeeds after north deletion", not result.is_error)
                await delete_and_verify(http, client, ready[1], expect_notification=True)
                created = []
                check("tool catalog returned to baseline", await tool_names(client) == baseline)
        finally:
            cleanup: list[asyncio.Task] = []
            for row in created:
                current = next((item for item in (await http.get("/api/stores")).json() if item["id"] == row["id"]), None)
                if current and current["status"] not in ("deleting", "deleted"):
                    await http.delete(f"/api/stores/{row['id']}", headers=XRW)
                if current and current["status"] != "deleted":
                    cleanup.append(asyncio.create_task(wait_store(http, row["id"], "deleted", 360)))
            if cleanup:
                await asyncio.gather(*cleanup)
            if key_id:
                await http.delete(f"/api/keys/{key_id}", headers=XRW)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
