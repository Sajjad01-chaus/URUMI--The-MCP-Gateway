import uuid
from datetime import UTC, timedelta

from sqlalchemy import or_, select, update

from ..db import Database
from ..models import ManagedRelease, utcnow


class ReleaseRepository:
    def __init__(self, db: Database, worker_id: str, lease_seconds: int, max_attempts: int):
        self.db = db
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    async def claim(self, limit: int) -> list[ManagedRelease]:
        now = utcnow()
        async with self.db.session() as session:
            query = (
                select(ManagedRelease)
                .where(
                    ManagedRelease.status.in_(("provisioning", "deleting")),
                    or_(ManagedRelease.lease_until.is_(None), ManagedRelease.lease_until < now),
                )
                .order_by(ManagedRelease.created_at)
                .limit(limit)
            )
            if session.bind and session.bind.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            candidates = list((await session.scalars(query)).all())
            claimed: list[ManagedRelease] = []
            for row in candidates:
                deadline = row.deadline_at if row.deadline_at.tzinfo else row.deadline_at.replace(tzinfo=UTC)
                if row.status == "provisioning" and deadline <= now:
                    row.status = "failed"
                    row.status_reason = "provisioning deadline expired before work could start"
                    row.lease_owner = None
                    row.lease_until = None
                    continue
                result = await session.execute(
                    update(ManagedRelease)
                    .where(
                        ManagedRelease.id == row.id,
                        or_(ManagedRelease.lease_until.is_(None), ManagedRelease.lease_until < now),
                    )
                    .values(
                        lease_owner=self.worker_id,
                        lease_until=now + timedelta(seconds=self.lease_seconds),
                        attempts=ManagedRelease.attempts + (1 if row.status == "provisioning" else 0),
                        status_reason=(f"starting attempt {row.attempts + 1}" if row.status == "provisioning" else "starting teardown"),
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 1:
                    row.lease_owner = self.worker_id
                    row.lease_until = now + timedelta(seconds=self.lease_seconds)
                    if row.status == "provisioning":
                        row.attempts += 1
                    claimed.append(row)
            await session.commit()
            return claimed

    async def renew(self, release_id: uuid.UUID) -> str | None:
        now = utcnow()
        async with self.db.session() as session:
            row = await session.scalar(
                select(ManagedRelease).where(
                    ManagedRelease.id == release_id,
                    ManagedRelease.lease_owner == self.worker_id,
                )
            )
            if row is None:
                return None
            row.lease_until = now + timedelta(seconds=self.lease_seconds)
            row.updated_at = now
            await session.commit()
            return row.desired

    async def status(self, release_id: uuid.UUID, reason: str) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(ManagedRelease)
                .where(ManagedRelease.id == release_id, ManagedRelease.lease_owner == self.worker_id)
                .values(status_reason=reason[:1000], updated_at=utcnow())
            )
            await session.commit()

    async def ready(self, release_id: uuid.UUID) -> None:
        now = utcnow()
        async with self.db.session() as session:
            await session.execute(
                update(ManagedRelease)
                .where(ManagedRelease.id == release_id, ManagedRelease.lease_owner == self.worker_id)
                .values(
                    status="ready",
                    status_reason="store resources are ready",
                    ready_at=now,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            await session.commit()

    async def deleted(self, release_id: uuid.UUID) -> None:
        now = utcnow()
        async with self.db.session() as session:
            await session.execute(
                update(ManagedRelease)
                .where(ManagedRelease.id == release_id, ManagedRelease.lease_owner == self.worker_id)
                .values(
                    status="deleted",
                    status_reason="resources removed",
                    server_id=None,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            await session.commit()

    async def queue_delete(self, release_id: uuid.UUID) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(ManagedRelease)
                .where(ManagedRelease.id == release_id)
                .values(
                    desired="absent",
                    status="deleting",
                    status_reason="provisioning cancelled; queued for deletion",
                    lease_owner=None,
                    lease_until=None,
                    updated_at=utcnow(),
                )
            )
            await session.commit()

    async def failed_attempt(self, release_id: uuid.UUID, reason: str) -> str:
        now = utcnow()
        async with self.db.session() as session:
            row = await session.get(ManagedRelease, release_id)
            if row is None:
                return "missing"
            if row.desired == "absent":
                row.status = "deleting"
                row.status_reason = "provisioning stopped; queued for deletion"
                row.lease_owner = None
                row.lease_until = None
                await session.commit()
                return "deleting"
            deadline = row.deadline_at if row.deadline_at.tzinfo else row.deadline_at.replace(tzinfo=UTC)
            exhausted = row.attempts >= self.max_attempts or deadline <= now
            if exhausted:
                row.status = "failed"
                row.status_reason = reason[:1000]
                row.lease_owner = None
                row.lease_until = None
                await session.commit()
                return "failed"
            delay = 15 * (2 ** max(row.attempts - 1, 0))
            row.status_reason = f"attempt {row.attempts} failed; retrying in {delay}s: {reason}"[:1000]
            row.lease_owner = None
            row.lease_until = now + timedelta(seconds=delay)
            await session.commit()
            return "retrying"

    async def failed_teardown(self, release_id: uuid.UUID, reason: str) -> None:
        now = utcnow()
        async with self.db.session() as session:
            await session.execute(
                update(ManagedRelease)
                .where(ManagedRelease.id == release_id)
                .values(
                    status="deleting",
                    status_reason=f"teardown retry pending: {reason}"[:1000],
                    lease_owner=None,
                    lease_until=now + timedelta(seconds=15),
                    updated_at=now,
                )
            )
            await session.commit()
