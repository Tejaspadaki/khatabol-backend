"""Scheduled jobs: morning reminders, evening voice summary, nightly balance verification."""

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.config import get_settings
from app.db.base import get_sessionmaker
from app.db.models import Merchant
from app.events import bus
from app.i18n import t
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.nlp.hinglish_numbers import format_rupees
from app.scheduler.reminders import send_reminders

log = logging.getLogger("khata.scheduler")


def summary_text(s: dict, lang: str = "hi") -> str:
    return t(lang, "daily_summary", credit=format_rupees(s["credit_given_paise"]),
             collected=format_rupees(s["collected_paise"]), outstanding=format_rupees(s["outstanding_paise"]))


async def _merchants(session) -> list[Merchant]:
    return list((await session.execute(select(Merchant))).scalars())


async def morning_reminders() -> None:
    today = datetime.now(get_settings().tz).date()
    async with get_sessionmaker()() as session:
        for m in await _merchants(session):
            rows = await ledger.list_dues(session, m.id, "today", today)
            try:
                sent = await send_reminders(
                    session, m, [(r["customer"], r["balance_paise"], r["next_due_date"]) for r in rows]
                )
            except LedgerError as exc:
                log.warning("reminders skipped for %s: %s", m.id, exc)
                continue
            await session.commit()
            if sent:
                bus.publish(m.id, "reminders_sent", t(m.language, "reminders_sent", n=len(sent)))


async def evening_summary() -> None:
    today = datetime.now(get_settings().tz).date()
    async with get_sessionmaker()() as session:
        for m in await _merchants(session):
            s = await ledger.daily_summary(session, m.id, today)
            bus.publish(m.id, "daily_summary", summary_text(s, m.language))


async def nightly_verify() -> None:
    async with get_sessionmaker()() as session:
        fixed = await ledger.verify_balances(session)
        await session.commit()
        if fixed:
            log.error("balance drift fixed: %s", fixed)


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    sched = AsyncIOScheduler(timezone=s.tz)
    sched.add_job(morning_reminders, CronTrigger(hour=s.reminder_hour, minute=0, timezone=s.tz))
    sched.add_job(evening_summary, CronTrigger(hour=s.summary_hour, minute=0, timezone=s.tz))
    sched.add_job(nightly_verify, CronTrigger(hour=2, minute=30, timezone=s.tz))
    return sched
