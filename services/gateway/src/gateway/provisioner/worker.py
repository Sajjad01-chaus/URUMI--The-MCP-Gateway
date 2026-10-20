import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from ..config import Settings
from ..models import ManagedRelease
from ..security import CredentialBox
from .helm import HelmClient, HelmError
from .kubernetes import KubernetesClient, KubernetesError
from .repository import ReleaseRepository

log = logging.getLogger(__name__)


class DeletionRequested(RuntimeError):
    pass


def seconds_until(deadline: datetime) -> int:
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(0, int((deadline - datetime.now(UTC)).total_seconds()))


class Provisioner:
    def __init__(
        self,
        settings: Settings,
        repository: ReleaseRepository,
        helm: HelmClient,
        kube: KubernetesClient,
        box: CredentialBox,
    ):
        self.settings = settings
        self.repository = repository
        self.helm = helm
        self.kube = kube
        self.box = box
        self.tasks: dict[object, asyncio.Task] = {}

    async def run(self) -> None:
        log.info("provisioner started with concurrency=%d", self.settings.provisioner_concurrency)
        while True:
            await self._touch("provisioner-heartbeat")
            self.tasks = {release_id: task for release_id, task in self.tasks.items() if not task.done()}
            free = self.settings.provisioner_concurrency - len(self.tasks)
            database_ok = True
            if free > 0:
                try:
                    for row in await self.repository.claim(free):
                        self.tasks[row.id] = asyncio.create_task(self.process(row), name=f"release-{row.slug}")
                except SQLAlchemyError:
                    database_ok = False
                    log.exception("failed to claim managed releases")
            if database_ok:
                await self._touch("provisioner-ready")
            await asyncio.sleep(self.settings.provisioner_poll_seconds)

    async def _touch(self, name: str) -> None:
        path = Path(self.settings.provisioner_temp_dir) / name
        await asyncio.to_thread(path.touch)

    async def _with_lease(self, row: ManagedRelease, operation) -> None:
        task = asyncio.create_task(operation)
        while True:
            done, _ = await asyncio.wait({task}, timeout=self.settings.provisioner_lease_renew_seconds)
            if done:
                await task
                return
            desired = await self.repository.renew(row.id)
            if desired is None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise HelmError("lease was lost while operating on the release")
            if desired == "absent" and row.status == "provisioning":
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self.repository.queue_delete(row.id)
                raise DeletionRequested

    async def process(self, row: ManagedRelease) -> None:
        try:
            if row.desired == "absent" or row.status == "deleting":
                await self.repository.status(row.id, "uninstalling Helm release")
                namespace_state = await self.kube.namespace_state(row.namespace)
                if namespace_state == "active":
                    await self._with_lease(row, self.helm.uninstall(row, self.kube))
                while await self.kube.namespace_exists(row.namespace):
                    if seconds_until(row.deadline_at) == 0:
                        raise KubernetesError("namespace deletion exceeded its deadline")
                    await asyncio.sleep(2)
                await self.repository.deleted(row.id)
                log.info("deleted managed release %s", row.slug)
                return

            release_secrets = json.loads(self.box.decrypt(row.secrets_encrypted))
            await self.repository.status(
                row.id,
                "installing store chart" if row.kind == "store" else "installing hosted MCP chart",
            )
            timeout = seconds_until(row.deadline_at)
            if timeout == 0:
                raise HelmError("provisioning deadline expired")
            operation = (
                self.helm.install_store(row, release_secrets, self.kube, timeout)
                if row.kind == "store"
                else self.helm.install_hosted(row, release_secrets, self.kube, timeout)
            )
            await self._with_lease(row, operation)
            await self.repository.ready(row.id, row.kind)
            log.info("managed release %s is ready", row.slug)
        except DeletionRequested:
            log.info("managed release %s switched to deletion", row.slug)
        except asyncio.CancelledError:
            raise
        except (HelmError, KubernetesError, OSError, ValueError, json.JSONDecodeError) as exc:
            if row.desired == "absent" or row.status == "deleting":
                await self.repository.failed_teardown(row.id, str(exc))
                log.warning("teardown of %s failed: %s", row.slug, exc)
            else:
                outcome = await self.repository.failed_attempt(row.id, str(exc))
                log.warning("provisioning attempt for %s ended %s: %s", row.slug, outcome, exc)
        except Exception as exc:
            log.exception("unexpected provisioner failure for %s", row.slug)
            await self.repository.failed_attempt(row.id, f"unexpected provisioner error: {type(exc).__name__}")
