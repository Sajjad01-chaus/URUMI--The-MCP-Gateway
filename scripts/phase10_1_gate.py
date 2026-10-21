"""Create, crash-recover and delete one managed store through the public API."""

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx2

ROOT = Path(__file__).resolve().parent.parent
ENV = os.environ.get("URUMI_ENV", "local")
SECRETS = ROOT / "deploy" / "secrets" / f"{ENV}.secrets.yaml"
DOMAIN = sys.argv[1] if len(sys.argv) > 1 else "127-0-0-1.nip.io"
SCHEME = "https" if ENV == "prod" else "http"
APP = f"{SCHEME}://app.{DOMAIN}"
SLUG = "recovery"
NAMESPACE = f"store-{SLUG}"
XRW = {"X-Requested-With": "phase-10.1-gate"}
log = logging.getLogger(__name__)


class GateError(RuntimeError):
    pass


def secret_value(text: str, key: str) -> str:
    match = re.search(rf'^\s*{key}:\s*"?([^"\r\n]+)', text, re.MULTILINE)
    if not match:
        raise GateError(f"{key} is missing from {SECRETS}")
    return match.group(1).strip()


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=120, check=False)
    if check and result.returncode:
        raise GateError(f"kubectl {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}".rstrip(), flush=True)
    if not condition:
        raise GateError(f"{label}: {detail}")


async def wait_for_status(http: httpx2.AsyncClient, store_id: str, expected: str, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last = "missing"
    while time.monotonic() < deadline:
        response = await http.get("/api/stores") if expected != "deleted" else await http.get(
            "/api/stores", params={"include": "deleted"}
        )
        response.raise_for_status()
        store = next((item for item in response.json() if item["id"] == store_id), None)
        if store:
            last = f"{store['status']}: {store['reason']}"
            if store["status"] == expected:
                return store
            if expected == "ready" and store["status"] == "failed":
                raise GateError(last)
        print(f"  waiting for {expected}: {last}", flush=True)
        await asyncio.sleep(10)
    raise GateError(f"timed out waiting for {expected}; last state: {last}")


async def run() -> None:
    text = SECRETS.read_text(encoding="utf-8")
    async with httpx2.AsyncClient(base_url=APP, timeout=30) as http:
        login = await http.post(
            "/api/auth/login",
            json={
                "email": secret_value(text, "bootstrapAdminEmail"),
                "password": secret_value(text, "bootstrapAdminPassword"),
            },
        )
        check("admin login", login.status_code == 200, f"HTTP {login.status_code}")

        create = await http.post("/api/stores", json={"name": "Recovery Store", "slug": SLUG}, headers=XRW)
        check("create accepted", create.status_code in (200, 201), f"HTTP {create.status_code}")
        store = create.json()
        if store["status"] == "deleting":
            await wait_for_status(http, store["id"], "deleted", timeout_seconds=360)
            check("previous deletion settled", True)
            create = await http.post("/api/stores", json={"name": "Recovery Store", "slug": SLUG}, headers=XRW)
            check("fresh create accepted", create.status_code == 201, f"HTTP {create.status_code}")
            store = create.json()
        store_id = store["id"]
        needs_recovery = store["status"] != "ready"
        if store["status"] == "failed":
            retry = await http.post(f"/api/stores/{store_id}/retry", headers=XRW)
            check("failed store retried", retry.status_code == 200 and retry.json()["status"] == "provisioning")
        repeated = await http.post("/api/stores", json={"name": "Recovery Store", "slug": SLUG}, headers=XRW)
        check("create idempotent", repeated.status_code == 200 and repeated.json()["id"] == store_id)

        if needs_recovery:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline and kubectl("get", "namespace", NAMESPACE, check=False).returncode:  # noqa: ASYNC110
                await asyncio.sleep(2)
            check("managed namespace created", kubectl("get", "namespace", NAMESPACE, check=False).returncode == 0)
            await asyncio.sleep(20)
            before = kubectl(
                "get", "pod", "-n", "platform", "-l", "app.kubernetes.io/name=provisioner", "-o", "jsonpath={.items[0].metadata.uid}"
            ).stdout
            kubectl("delete", "pod", "-n", "platform", "-l", "app.kubernetes.io/name=provisioner", "--wait=false")
            check("provisioner killed mid-install", bool(before))

            store = await wait_for_status(http, store_id, "ready", timeout_seconds=720)
            after = kubectl(
                "get", "pod", "-n", "platform", "-l", "app.kubernetes.io/name=provisioner", "-o", "jsonpath={.items[0].metadata.uid}"
            ).stdout
            check("new provisioner recovered lease", bool(after) and after != before)
        check("store URL is stable", store["url"] == f"{SCHEME}://store-{SLUG}.{DOMAIN}")
        managed_by = kubectl(
            "get", "namespace", NAMESPACE, "-o", "jsonpath={.metadata.labels.urumi\\.io/managed-by}"
        ).stdout
        tenant = kubectl("get", "namespace", NAMESPACE, "-o", "jsonpath={.metadata.labels.urumi\\.io/tenant}").stdout
        check("namespace labels", managed_by == "provisioner" and tenant == SLUG, f"{managed_by}|{tenant}")
        check("tenant RoleBinding", kubectl("get", "rolebinding", "provisioner", "-n", NAMESPACE, check=False).returncode == 0)
        check("ResourceQuota", kubectl("get", "resourcequota", "tenant-quota", "-n", NAMESPACE, check=False).returncode == 0)
        check("LimitRange", kubectl("get", "limitrange", "tenant-limits", "-n", NAMESPACE, check=False).returncode == 0)
        async with httpx2.AsyncClient(timeout=30, follow_redirects=True) as public:
            storefront = await public.get(store["url"])
        check("storefront responds", storefront.status_code == 200, f"HTTP {storefront.status_code}")

        delete = await http.delete(f"/api/stores/{store_id}", headers=XRW)
        check("delete accepted", delete.status_code == 202, f"HTTP {delete.status_code}")
        await wait_for_status(http, store_id, "deleted", timeout_seconds=360)
        check("namespace removed", kubectl("get", "namespace", NAMESPACE, check=False).returncode != 0)
        helm = await asyncio.to_thread(
            subprocess.run,
            ["helm", "status", f"store-{SLUG}", "-n", NAMESPACE],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        check("Helm release removed", helm.returncode != 0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
