"""Alembic environment.

Two ways in:
- `uv run alembic upgrade head` from the CLI: builds an async engine from app settings.
- `app.db.migrate.upgrade_to_head()` at app start: passes an open sync connection in
  `config.attributes["connection"]`, so migrations run inside the app's event loop.
"""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import models  # noqa: F401  (registers tables)
from app.db.base import Base

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite can't ALTER most things in place
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    from app.config import get_settings

    engine = create_async_engine(context.config.attributes.get("url") or get_settings().database_url)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


def run_migrations_offline() -> None:
    from app.config import get_settings

    context.configure(url=get_settings().database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
elif (conn := context.config.attributes.get("connection")) is not None:
    _run(conn)
else:
    asyncio.run(_run_async())
