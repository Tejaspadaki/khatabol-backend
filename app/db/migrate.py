"""Run Alembic migrations from inside the app (startup) or tests."""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

BASELINE = "0001"
_ROOT = Path(__file__).resolve().parents[2]
_migration_lock = asyncio.Lock()


def alembic_config() -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "app" / "db" / "migrations"))
    return cfg


def _upgrade(conn: Connection) -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    tables = set(inspect(conn).get_table_names())
    if "alembic_version" not in tables and "merchants" in tables:
        # Created by the old create_all() boot path, before migrations existed: it matches the baseline.
        command.stamp(cfg, BASELINE)
    command.upgrade(cfg, "head")


async def upgrade_to_head(engine: AsyncEngine) -> None:
    async with _migration_lock:
        async with engine.begin() as conn:
            await conn.run_sync(_upgrade)
