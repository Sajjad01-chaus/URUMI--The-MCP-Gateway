import json
import logging
from typing import Any

from .db import Database
from .models import AuditLog

log = logging.getLogger(__name__)


def args_preview(arguments: dict[str, Any] | None) -> dict[str, Any] | None:
    if arguments is None:
        return None
    text = json.dumps(arguments, default=str)
    return arguments if len(text) <= 1000 else {"_truncated": text[:1000]}


class Auditor:
    """Writes one row per tools/call. A failed write is logged but never breaks the call itself."""

    def __init__(self, db: Database):
        self.db = db

    async def record(self, **fields: Any) -> None:
        try:
            async with self.db.session() as session:
                session.add(AuditLog(**fields))
                await session.commit()
        except Exception:
            log.exception("audit write failed for %s", fields.get("exposed_name"))
