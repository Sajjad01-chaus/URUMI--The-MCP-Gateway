import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..models import ManagedRelease
from .kubernetes import KubernetesClient, KubernetesError

log = logging.getLogger(__name__)


class HelmError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str


def mask(text: str, secret_values: list[str]) -> str:
    masked = text
    for value in sorted((value for value in secret_values if value), key=len, reverse=True):
        masked = masked.replace(value, "[REDACTED]")
    return masked


class CommandRunner:
    async def run(self, args: list[str], secret_values: list[str] | None = None) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
            raise
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        values = secret_values or []
        if process.returncode:
            detail = mask(stderr or stdout, values).strip()[-2000:]
            raise HelmError(f"command failed ({process.returncode}): {detail}")
        if stderr.strip():
            log.info("helm: %s", mask(stderr, values).strip())
        return CommandResult(mask(stdout, values), mask(stderr, values))


class HelmClient:
    def __init__(self, settings: Settings, runner: CommandRunner | None = None):
        self.settings = settings
        self.runner = runner or CommandRunner()

    async def recover_pending(self, row: ManagedRelease) -> None:
        try:
            result = await self.runner.run(["helm", "status", row.release, "-n", row.namespace, "-o", "json"])
        except HelmError as exc:
            if "not found" in str(exc).lower():
                return
            raise
        status = json.loads(result.stdout).get("info", {}).get("status", "")
        if status == "pending-install":
            await self.runner.run(["helm", "uninstall", row.release, "-n", row.namespace, "--keep-history"])
        elif status == "pending-upgrade":
            history = await self.runner.run(["helm", "history", row.release, "-n", row.namespace, "-o", "json"])
            revisions = [item["revision"] for item in json.loads(history.stdout) if item.get("status") == "deployed"]
            if not revisions:
                raise HelmError("pending upgrade has no deployed revision to roll back to")
            await self.runner.run(["helm", "rollback", row.release, str(revisions[-1]), "-n", row.namespace, "--wait"])

    def dynamic_values(self, row: ManagedRelease, release_secrets: dict[str, str]) -> dict:
        return {
            "global": {
                "domain": self.settings.public_domain,
                "tls": {
                    "enabled": self.settings.provisioner_tls_enabled,
                    "clusterIssuer": self.settings.provisioner_tls_issuer,
                },
            },
            "store": {"subdomain": f"store-{row.slug}", "title": row.display_name},
            "wooMcp": {"image": self.settings.provisioner_woo_mcp_image},
            "secrets": {"create": True, **release_secrets},
        }

    async def install_store(
        self,
        row: ManagedRelease,
        release_secrets: dict[str, str],
        kube: KubernetesClient,
        timeout_seconds: int,
    ) -> None:
        await kube.ensure_tenant_namespace(row.namespace, row.slug)
        await self.recover_pending(row)
        values = self.dynamic_values(row, release_secrets)
        secret_values = list(release_secrets.values())
        fd, values_path = tempfile.mkstemp(
            prefix=f"{row.slug}-", suffix=".json", dir=self.settings.provisioner_temp_dir
        )
        try:
            os.chmod(values_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(values, handle)
            await self.runner.run(
                [
                    "helm",
                    "upgrade",
                    "--install",
                    row.release,
                    str(Path(self.settings.provisioner_chart_root) / "woo-store"),
                    "-n",
                    row.namespace,
                    "-f",
                    self.settings.provisioner_tenant_values,
                    "-f",
                    values_path,
                    "--wait",
                    "--timeout",
                    f"{max(timeout_seconds, 60)}s",
                ],
                secret_values,
            )
        finally:
            await asyncio.to_thread(Path(values_path).unlink, missing_ok=True)
        await kube.verify_store(row.namespace)

    def hosted_values(self, row: ManagedRelease, release_secrets: dict[str, str]) -> dict:
        image, tag = row.spec["image"].rsplit(":", 1)
        resources = {
            "small": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "500m", "memory": "256Mi"},
            },
            "medium": {
                "requests": {"cpu": "250m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "512Mi"},
            },
        }[row.spec.get("size", "small")]
        return {
            "image": {"repository": image, "tag": tag},
            "mcp": {
                "port": row.spec["port"],
                "env": row.spec.get("env", {}),
                "secretEnv": release_secrets,
            },
            "internetEgress": row.spec.get("internet_egress", False),
            "resources": resources,
        }

    async def install_hosted(
        self,
        row: ManagedRelease,
        release_secrets: dict[str, str],
        kube: KubernetesClient,
        timeout_seconds: int,
    ) -> None:
        await kube.ensure_tenant_namespace(row.namespace, row.slug)
        await self.recover_pending(row)
        values = self.hosted_values(row, release_secrets)
        secret_values = list(release_secrets.values())
        fd, values_path = tempfile.mkstemp(
            prefix=f"{row.slug}-", suffix=".json", dir=self.settings.provisioner_temp_dir
        )
        try:
            os.chmod(values_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(values, handle)
            try:
                await self.runner.run(
                    [
                        "helm", "upgrade", "--install", row.release,
                        str(Path(self.settings.provisioner_chart_root) / "hosted-mcp"),
                        "-n", row.namespace, "-f", values_path,
                    ],
                    secret_values,
                )
                await kube.wait_hosted_ready(row.namespace, timeout_seconds)
            except (HelmError, KubernetesError) as exc:
                diagnosis = await kube.hosted_failure_reason(row.namespace)
                raise HelmError(diagnosis or str(exc)) from exc
        finally:
            await asyncio.to_thread(Path(values_path).unlink, missing_ok=True)

    async def uninstall(self, row: ManagedRelease, kube: KubernetesClient) -> None:
        try:
            await self.runner.run(["helm", "uninstall", row.release, "-n", row.namespace, "--wait", "--timeout", "4m"])
        except HelmError as exc:
            if "not found" not in str(exc).lower():
                raise
        await kube.delete_namespace(row.namespace)
