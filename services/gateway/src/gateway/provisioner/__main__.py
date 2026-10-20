import asyncio
import logging
import os
import socket

from prometheus_client import start_http_server

from ..config import get_settings
from ..db import Database
from ..metrics import ProvisionerMetrics
from ..security import CredentialBox
from .helm import HelmClient
from .kubernetes import KubernetesClient
from .repository import ReleaseRepository
from .worker import Provisioner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def run() -> None:
    settings = get_settings()
    db = Database(settings.database_url)
    kube = KubernetesClient.in_cluster()
    worker_id = os.environ.get("HOSTNAME", socket.gethostname())
    repository = ReleaseRepository(
        db,
        worker_id=worker_id,
        lease_seconds=settings.provisioner_lease_seconds,
        max_attempts=settings.provisioner_max_attempts,
    )
    metrics = ProvisionerMetrics()
    start_http_server(settings.provisioner_metrics_port, addr="0.0.0.0", registry=metrics.registry)
    provisioner = Provisioner(
        settings, repository, HelmClient(settings), kube, CredentialBox(settings.credential_keyring), metrics
    )
    try:
        await provisioner.run()
    finally:
        await kube.close()
        await db.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
