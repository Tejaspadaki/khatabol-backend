"""Alembic migrations: a fresh database upgrades to head and matches the models exactly."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from app.db.migrate import alembic_config, upgrade_to_head


def _diff(conn):
    ctx = MigrationContext.configure(conn, opts={"compare_type": True, "render_as_batch": True})
    return compare_metadata(ctx, Base.metadata)


def _state(conn):
    insp = inspect(conn)
    version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    return version, {c["name"] for c in insp.get_columns("customers")}, set(insp.get_table_names())


async def test_fresh_database_upgrades_to_models(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    await upgrade_to_head(engine)
    async with engine.connect() as conn:
        assert await conn.run_sync(_diff) == []
        version, _, _ = await conn.run_sync(_state)
    await engine.dispose()
    assert version == ScriptDirectory.from_config(alembic_config()).get_current_head()


async def test_database_from_old_create_all_is_stamped_then_upgraded(tmp_path):
    """khata.db files made before migrations existed match the baseline; they get stamped and upgraded."""
    from alembic import command

    url = f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}"
    engine = create_async_engine(url)

    def baseline_only(conn):
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
        conn.exec_driver_sql("DROP TABLE alembic_version")  # what create_all() used to leave behind

    async with engine.begin() as conn:
        await conn.run_sync(baseline_only)
    await upgrade_to_head(engine)
    async with engine.connect() as conn:
        assert await conn.run_sync(_diff) == []
        _, customer_cols, tables = await conn.run_sync(_state)
    await engine.dispose()
    assert "credit_limit_paise" in customer_cols and "customer_events" in tables


async def test_0005_backfills_allocations_oldest_due_first(tmp_path):
    """A khata from before payment_allocations gets its rows: the payment pays the oldest-due udhaar."""
    import uuid as _uuid

    from alembic import command

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    m, c, late_due, early_due, pay = (_uuid.uuid4() for _ in range(5))

    def at_0004_with_data(conn):
        cfg = alembic_config()
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0004")
        q = conn.exec_driver_sql
        q("INSERT INTO merchants (id, name, language, large_amount_threshold_paise, created_at, hold_grace_days) "
          "VALUES (?, 'Shop', 'hi', 1000000, '2026-10-01 10:00:00', 15)", (m.hex,))
        q("INSERT INTO customers (id, merchant_id, name, name_normalized, phonetic_key, nicknames, ref_code, balance_paise,"
          " created_at, on_hold) VALUES (?, ?, 'Ramesh', 'ramesh', 'RMS', '[]', 'AAAAA', 10000, '2026-10-01 10:00:00', 0)",
          (c.hex, m.hex))
        for eid, kind, amt, due, at in ((late_due, "CREDIT", 10000, "2026-12-01", "2026-10-02 10:00:00"),
                                        (early_due, "CREDIT", 10000, "2026-11-01", "2026-10-03 10:00:00"),
                                        (pay, "PAYMENT", 10000, None, "2026-10-04 10:00:00")):
            q("INSERT INTO ledger_entries (id, merchant_id, customer_id, type, amount_paise, due_date, source, created_at)"
              " VALUES (?, ?, ?, ?, ?, ?, 'MANUAL', ?)", (eid.hex, m.hex, c.hex, kind, amt, due, at))

    async with engine.begin() as conn:
        await conn.run_sync(at_0004_with_data)
    await upgrade_to_head(engine)
    async with engine.connect() as conn:
        rows = (await conn.exec_driver_sql(
            "SELECT payment_entry_id, credit_entry_id, amount_paise FROM payment_allocations")).all()
    await engine.dispose()
    assert rows == [(pay.hex, early_due.hex, 10000)]
