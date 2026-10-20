import json
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
from sqlalchemy import select, update

from gateway.config import Settings
from gateway.db import Database
from gateway.models import Base, ManagedRelease, User, utcnow
from gateway.provisioner.helm import CommandResult, HelmClient, HelmError, mask
from gateway.provisioner.repository import ReleaseRepository
from gateway.provisioner.worker import Provisioner


def release(owner_id: uuid.UUID, slug: str = "london") -> ManagedRelease:
    return ManagedRelease(
        kind="store",
        slug=slug,
        display_name=slug.title(),
        namespace=f"store-{slug}",
        release=f"store-{slug}",
        owner_id=owner_id,
        desired="present",
        status="provisioning",
        status_reason="queued",
        spec={"profile": "tenant"},
        secrets_encrypted=b"encrypted",
        deadline_at=utcnow() + timedelta(minutes=15),
    )


async def database(tmp_path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'provisioner.db'}")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return db


async def test_lease_claim_is_exclusive_and_expired_work_is_recovered(tmp_path):
    db = await database(tmp_path)
    async with db.session() as session:
        owner = User(email="owner@x.io", role="member")
        session.add(owner)
        await session.flush()
        row = release(owner.id)
        session.add(row)
        await session.commit()
        release_id = row.id

    first = ReleaseRepository(db, "worker-a", lease_seconds=60, max_attempts=3)
    second = ReleaseRepository(db, "worker-b", lease_seconds=60, max_attempts=3)
    claimed = await first.claim(1)
    assert [row.id for row in claimed] == [release_id] and claimed[0].attempts == 1
    assert await second.claim(1) == []

    async with db.session() as session:
        await session.execute(
            update(ManagedRelease).where(ManagedRelease.id == release_id).values(lease_until=utcnow() - timedelta(seconds=1))
        )
        await session.commit()
    recovered = await second.claim(1)
    assert [row.id for row in recovered] == [release_id] and recovered[0].attempts == 2
    await db.dispose()


async def test_retry_backoff_then_attempt_exhaustion(tmp_path):
    db = await database(tmp_path)
    async with db.session() as session:
        owner = User(email="owner@x.io", role="member")
        session.add(owner)
        await session.flush()
        row = release(owner.id)
        row.attempts = 1
        row.lease_owner = "worker"
        session.add(row)
        await session.commit()
        release_id = row.id

    repository = ReleaseRepository(db, "worker", lease_seconds=60, max_attempts=3)
    assert await repository.failed_attempt(release_id, "temporary") == "retrying"
    async with db.session() as session:
        row = await session.get(ManagedRelease, release_id)
        assert row.status == "provisioning" and row.lease_until > utcnow().replace(tzinfo=None)
        row.attempts = 3
        row.lease_owner = "worker"
        await session.commit()
    assert await repository.failed_attempt(release_id, "final") == "failed"
    async with db.session() as session:
        row = await session.get(ManagedRelease, release_id)
        assert row.status == "failed" and row.status_reason == "final" and row.lease_owner is None
    await db.dispose()


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.values_path: Path | None = None
        self.values: dict | None = None

    async def run(self, args: list[str], secret_values=None) -> CommandResult:
        self.calls.append(args)
        if args[1] == "status":
            raise HelmError("release: not found")
        if args[1] == "upgrade":
            values_index = max(index for index, value in enumerate(args) if value == "-f")
            self.values_path = Path(args[values_index + 1])
            self.values = json.loads(self.values_path.read_text(encoding="utf-8"))
        return CommandResult("", "")


class FakeKubernetes:
    def __init__(self):
        self.ensured: tuple[str, str] | None = None
        self.verified: str | None = None
        self.hosted_verified: str | None = None

    async def ensure_tenant_namespace(self, namespace: str, slug: str) -> None:
        self.ensured = (namespace, slug)

    async def verify_store(self, namespace: str) -> None:
        self.verified = namespace

    async def verify_hosted(self, namespace: str) -> None:
        self.hosted_verified = namespace

    async def wait_hosted_ready(self, namespace: str, timeout_seconds: int) -> None:
        await self.verify_hosted(namespace)


async def test_helm_values_use_a_mode_600_temporary_file_and_mask_secrets(tmp_path):
    key = Fernet.generate_key().decode()
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        credential_encryption_key=key,
        provisioner_temp_dir=str(tmp_path),
        provisioner_chart_root="/charts",
        provisioner_tenant_values="/charts/woo-store/values-tenant.yaml",
    )
    runner = FakeRunner()
    kube = FakeKubernetes()
    helm = HelmClient(settings, runner=runner)
    row = release(uuid.uuid4())
    store_secrets = {"mysqlPassword": "TOP-SECRET", "wooMcpToken": "TOKEN"}
    await helm.install_store(row, store_secrets, kube, timeout_seconds=120)

    assert kube.ensured == ("store-london", "london") and kube.verified == "store-london"
    assert runner.values["secrets"]["mysqlPassword"] == "TOP-SECRET"
    assert runner.values_path and not runner.values_path.exists()
    assert "TOP-SECRET" not in " ".join(part for call in runner.calls for part in call)
    assert mask("failed TOP-SECRET TOKEN", list(store_secrets.values())) == "failed [REDACTED] [REDACTED]"


async def test_hosted_helm_values_are_hardened_inputs_and_secrets_are_temporary(tmp_path):
    key = Fernet.generate_key().decode()
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        credential_encryption_key=key,
        provisioner_temp_dir=str(tmp_path),
        provisioner_chart_root="/charts",
    )
    runner = FakeRunner()
    kube = FakeKubernetes()
    helm = HelmClient(settings, runner=runner)
    row = release(uuid.uuid4(), "clock")
    row.kind = "hosted_mcp"
    row.namespace = "mcp-clock"
    row.release = "mcp-clock"
    row.spec = {
        "image": "urumi/fx-mcp:dev",
        "port": 8000,
        "path": "/mcp",
        "env": {"LOG_LEVEL": "info"},
        "size": "medium",
        "internet_egress": False,
    }
    await helm.install_hosted(row, {"MCP_TOKEN": "TOP-SECRET"}, kube, timeout_seconds=120)

    assert kube.ensured == ("mcp-clock", "clock") and kube.hosted_verified == "mcp-clock"
    assert runner.values["image"] == {"repository": "urumi/fx-mcp", "tag": "dev"}
    assert runner.values["mcp"]["secretEnv"] == {"MCP_TOKEN": "TOP-SECRET"}
    assert runner.values["resources"]["limits"]["memory"] == "512Mi"
    assert runner.values_path and not runner.values_path.exists()
    assert "TOP-SECRET" not in " ".join(part for call in runner.calls for part in call)


async def test_deleted_slug_can_be_reused(tmp_path):
    db = await database(tmp_path)
    async with db.session() as session:
        owner = User(email="owner@x.io", role="member")
        session.add(owner)
        await session.flush()
        old = release(owner.id)
        old.status = "deleted"
        old.desired = "absent"
        session.add_all([old, release(owner.id)])
        await session.commit()
        rows = list((await session.scalars(select(ManagedRelease))).all())
    assert len(rows) == 2
    await db.dispose()


async def test_teardown_skips_helm_when_namespace_is_already_absent(tmp_path):
    settings = Settings(
        database_url="sqlite+aiosqlite://",
        credential_encryption_key=Fernet.generate_key().decode(),
        provisioner_temp_dir=str(tmp_path),
    )
    repository = Mock()
    repository.status = AsyncMock()
    repository.deleted = AsyncMock()
    helm = Mock()
    helm.uninstall = AsyncMock()
    kube = Mock()
    kube.namespace_state = AsyncMock(return_value="absent")
    kube.namespace_exists = AsyncMock(return_value=False)
    row = release(uuid.uuid4())
    row.id = uuid.uuid4()
    row.desired = "absent"
    row.status = "deleting"

    provisioner = Provisioner(settings, repository, helm, kube, Mock())
    await provisioner.process(row)

    helm.uninstall.assert_not_awaited()
    repository.deleted.assert_awaited_once_with(row.id)
