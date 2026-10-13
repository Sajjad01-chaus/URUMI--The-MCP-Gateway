import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.models import Base

# Apply alembic.ini's [loggers] section; without this Alembic's INFO lines are dropped.
if context.config.config_file_name:
    fileConfig(context.config.config_file_name)

target_metadata = Base.metadata
MIGRATION_LOCK_ID = 727274  # Postgres advisory lock: only one replica migrates at a time.


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        if connection.dialect.name == "postgresql":
            connection.execute(text(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})"))
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


asyncio.run(run_migrations_online())
