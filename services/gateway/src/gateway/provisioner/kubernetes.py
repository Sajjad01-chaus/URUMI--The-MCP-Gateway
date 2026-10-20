import asyncio
import os
from pathlib import Path
from typing import Any

import httpx2


class KubernetesError(RuntimeError):
    pass


class KubernetesClient:
    def __init__(self, client: httpx2.AsyncClient):
        self.client = client

    @classmethod
    def in_cluster(cls) -> "KubernetesClient":
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise KubernetesError("KUBERNETES_SERVICE_HOST is not set")
        token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
        ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise KubernetesError(f"cannot read service account token: {exc}") from exc
        client = httpx2.AsyncClient(
            base_url=f"https://{host}:{port}",
            headers={"Authorization": f"Bearer {token}"},
            verify=str(ca_path),
            timeout=10,
        )
        return cls(client)

    async def close(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        allow: tuple[int, ...] = (200, 201, 202),
    ) -> dict[str, Any] | None:
        response = await self.client.request(method, path, json=body)
        if response.status_code == 404 and 404 in allow:
            return None
        if response.status_code not in allow:
            detail = response.text.replace("\n", " ")[:500]
            raise KubernetesError(f"Kubernetes API {method} {path} returned {response.status_code}: {detail}")
        return response.json() if response.content else None

    async def ensure_tenant_namespace(self, namespace: str, slug: str) -> None:
        path = f"/api/v1/namespaces/{namespace}"
        current = await self.request("GET", path, allow=(200, 404))
        labels = {
            "urumi.io/managed-by": "provisioner",
            "urumi.io/mcp-upstreams": "true",
            "urumi.io/tenant": slug,
        }
        if current is None:
            await self.request(
                "POST",
                "/api/v1/namespaces",
                body={"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": labels}},
            )
        elif current.get("metadata", {}).get("labels", {}).get("urumi.io/managed-by") != "provisioner":
            raise KubernetesError(f"namespace {namespace} exists but is not provisioner-managed")

        binding_path = f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings/provisioner"
        if await self.request("GET", binding_path, allow=(200, 404)) is None:
            await self.request(
                "POST",
                f"/apis/rbac.authorization.k8s.io/v1/namespaces/{namespace}/rolebindings",
                body={
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": "provisioner", "namespace": namespace},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "urumi-tenant-admin"},
                    "subjects": [
                        {"kind": "ServiceAccount", "name": "provisioner", "namespace": "platform"}
                    ],
                },
            )

    async def verify_store(self, namespace: str) -> None:
        job = await self.request("GET", f"/apis/batch/v1/namespaces/{namespace}/jobs/store-bootstrap")
        if not job or job.get("status", {}).get("succeeded", 0) < 1:
            raise KubernetesError("bootstrap Job did not complete")
        endpoints = await self.request("GET", f"/api/v1/namespaces/{namespace}/endpoints/woo-mcp")
        ready = any(subset.get("addresses") for subset in (endpoints or {}).get("subsets", []))
        if not ready:
            raise KubernetesError("woo-mcp Service has no ready endpoint")
        try:
            response = await self.client.get(f"http://wordpress.{namespace}.svc.cluster.local", timeout=10)
            response.raise_for_status()
        except httpx2.HTTPError as exc:
            raise KubernetesError(f"storefront readiness request failed: {exc}") from exc

    async def verify_hosted(self, namespace: str) -> None:
        deployment = await self.request("GET", f"/apis/apps/v1/namespaces/{namespace}/deployments/mcp")
        if not deployment or deployment.get("status", {}).get("availableReplicas", 0) < 1:
            raise KubernetesError("hosted MCP Deployment has no available replica")
        endpoints = await self.request("GET", f"/api/v1/namespaces/{namespace}/endpoints/mcp")
        ready = any(subset.get("addresses") for subset in (endpoints or {}).get("subsets", []))
        if not ready:
            raise KubernetesError("hosted MCP Service has no ready endpoint")

    async def wait_hosted_ready(self, namespace: str, timeout_seconds: int) -> None:
        deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 1)
        while asyncio.get_running_loop().time() < deadline:
            try:
                await self.verify_hosted(namespace)
                return
            except KubernetesError as exc:
                reason = await self.hosted_failure_reason(namespace)
                if reason and reason.startswith("container must run as non-root"):
                    raise KubernetesError(reason) from exc
            await asyncio.sleep(2)
        reason = await self.hosted_failure_reason(namespace)
        raise KubernetesError(reason or "hosted MCP pod did not become ready before its deadline")

    async def hosted_failure_reason(self, namespace: str) -> str | None:
        pods = await self.request(
            "GET",
            f"/api/v1/namespaces/{namespace}/pods?labelSelector=app.kubernetes.io%2Fname%3Dmcp",
        )
        messages: list[str] = []
        pod_names: set[str] = set()
        for pod in (pods or {}).get("items", []):
            name = pod.get("metadata", {}).get("name")
            if name:
                pod_names.add(name)
            status = pod.get("status", {})
            if status.get("message"):
                messages.append(status["message"])
            for container in status.get("containerStatuses", []):
                waiting = container.get("state", {}).get("waiting", {})
                if waiting.get("message"):
                    messages.append(waiting["message"])
        events = await self.request("GET", f"/api/v1/namespaces/{namespace}/events")
        for event in (events or {}).get("items", []):
            involved = event.get("involvedObject", {})
            if involved.get("kind") == "Pod" and involved.get("name") in pod_names and event.get("message"):
                messages.append(event["message"])
        detail = " | ".join(dict.fromkeys(messages))[-1500:]
        lower = detail.lower()
        if "runasnonroot" in lower or "non-numeric user" in lower or "will run as root" in lower:
            return f"container must run as non-root: {detail}"
        return f"hosted MCP pod did not become ready: {detail}" if detail else None

    async def delete_namespace(self, namespace: str) -> None:
        await self.request(
            "DELETE",
            f"/api/v1/namespaces/{namespace}",
            body={"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground"},
            allow=(200, 202, 404),
        )

    async def namespace_exists(self, namespace: str) -> bool:
        return await self.request("GET", f"/api/v1/namespaces/{namespace}", allow=(200, 404)) is not None

    async def namespace_state(self, namespace: str) -> str:
        current = await self.request("GET", f"/api/v1/namespaces/{namespace}", allow=(200, 404))
        if current is None:
            return "absent"
        if current.get("metadata", {}).get("deletionTimestamp"):
            return "terminating"
        return "active"
