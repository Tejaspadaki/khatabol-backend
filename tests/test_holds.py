"""Feature 2: hold for overdue customers (BUILD_CONTEXT.md §11.2)."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.models import CustomerEvent, LedgerEntry, PendingAction
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.scheduler.reminders import send_reminders
from tests.conftest import FakeLLM, last_results, text, tool

NOW = datetime(2026, 10, 29, 11, 0, tzinfo=get_settings().tz)
TODAY = NOW.date()


async def _customer(session, merchant, name="Ramesh Patil", overdue_days: int | None = None, amount=50000):
    """A customer with one udhaar due `overdue_days` before TODAY (None = no due date)."""
    c = await ledger.create_customer(session, merchant.id, name, "9822001234")
    due = TODAY - timedelta(days=overdue_days) if overdue_days is not None else None
    await ledger.add_credit(session, merchant.id, c.id, amount, due_date=due)
    await session.commit()
    return c


async def _events(session, customer_id):
    stmt = select(CustomerEvent).where(CustomerEvent.customer_id == customer_id,
                                       CustomerEvent.event_type.in_(["HOLD_PLACED", "HOLD_LIFTED"]))
    return list((await session.execute(stmt.order_by(CustomerEvent.created_at))).scalars())


# ---------------------------------------------------------------- daily job

async def test_job_holds_customer_overdue_beyond_grace(session, merchant):
    c = await _customer(session, merchant, overdue_days=16)
    held = await ledger.apply_overdue_holds(session, TODAY)
    await session.commit()
    assert [h.id for h in held] == [c.id]
    await session.refresh(c)
    assert c.on_hold and "₹500" in c.hold_reason
    [ev] = await _events(session, c.id)
    assert ev.event_type == "HOLD_PLACED" and ev.actor == "system"
    assert ev.payload["overdue_paise"] == 50000 and ev.payload["reason"] == c.hold_reason


async def test_job_grace_boundary(session, merchant):
    at_grace = await _customer(session, merchant, "Ramesh Patil", overdue_days=15)
    past_grace = await _customer(session, merchant, "Sunita Devi", overdue_days=16)
    held = await ledger.apply_overdue_holds(session, TODAY)
    assert [h.id for h in held] == [past_grace.id]
    assert not at_grace.on_hold


async def test_job_uses_merchant_grace_days(session, merchant):
    merchant.hold_grace_days = 3
    c = await _customer(session, merchant, overdue_days=4)
    assert [h.id for h in await ledger.apply_overdue_holds(session, TODAY)] == [c.id]


async def test_no_due_date_never_holds(session, merchant):
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil")
    e = await ledger.add_credit(session, merchant.id, c.id, 50000)
    e.created_at = NOW - timedelta(days=400)
    await session.commit()
    assert await ledger.apply_overdue_holds(session, TODAY) == []


async def test_paid_overdue_credit_does_not_hold(session, merchant):
    c = await _customer(session, merchant, overdue_days=30)
    await ledger.record_payment(session, merchant.id, c.id, 50000)
    await session.commit()
    assert await ledger.apply_overdue_holds(session, TODAY) == []


async def test_job_is_idempotent(session, merchant):
    c = await _customer(session, merchant, overdue_days=20)
    await ledger.apply_overdue_holds(session, TODAY)
    assert await ledger.apply_overdue_holds(session, TODAY + timedelta(days=1)) == []
    assert len(await _events(session, c.id)) == 1


async def test_merchant_lift_is_respected_until_a_new_entry_goes_overdue(session, merchant):
    c = await _customer(session, merchant, overdue_days=20)
    await ledger.apply_overdue_holds(session, TODAY)
    await ledger.set_hold(session, merchant.id, c.id, False, "ghar mein shaadi hai", "merchant", TODAY)
    assert await ledger.apply_overdue_holds(session, TODAY + timedelta(days=1)) == []  # same entry: stays lifted

    await ledger.add_credit(session, merchant.id, c.id, 20000, due_date=TODAY + timedelta(days=2))
    assert await ledger.apply_overdue_holds(session, TODAY + timedelta(days=17)) == []  # day 15 after due
    held = await ledger.apply_overdue_holds(session, TODAY + timedelta(days=18))       # day 16: new overdue
    assert [h.id for h in held] == [c.id]
    assert [e.event_type for e in await _events(session, c.id)] == ["HOLD_PLACED", "HOLD_LIFTED", "HOLD_PLACED"]


# ---------------------------------------------------------------- what a hold blocks

async def test_hold_blocks_udhaar_only(session, merchant):
    c = await _customer(session, merchant, overdue_days=20)
    await ledger.apply_overdue_holds(session, TODAY)
    with pytest.raises(LedgerError) as exc:
        await ledger.add_credit(session, merchant.id, c.id, 1000)
    assert exc.value.key == "err_on_hold"
    await ledger.record_payment(session, merchant.id, c.id, 20000)  # payments work
    assert len(await ledger.get_history(session, merchant.id, c.id)) == 2  # history works
    sent = await send_reminders(session, merchant, [(c, c.balance_paise, None)])  # reminders work
    assert len(sent) == 1
    assert c.balance_paise == 30000


async def test_merchant_hold_and_lift_are_events(session, merchant):
    c = await _customer(session, merchant)
    await ledger.set_hold(session, merchant.id, c.id, True, "payment pe jhagda", "merchant", TODAY)
    assert c.on_hold and c.hold_reason == "payment pe jhagda"
    with pytest.raises(LedgerError) as exc:
        await ledger.set_hold(session, merchant.id, c.id, True, "again", "merchant", TODAY)
    assert exc.value.key == "err_already_on_hold"
    await ledger.set_hold(session, merchant.id, c.id, False, None, "merchant", TODAY)
    assert not c.on_hold and c.hold_reason is None
    with pytest.raises(LedgerError) as exc:
        await ledger.set_hold(session, merchant.id, c.id, False, None, "merchant", TODAY)
    assert exc.value.key == "err_not_on_hold"
    events = await _events(session, c.id)
    assert [(e.event_type, e.actor, e.payload["reason"]) for e in events] == [
        ("HOLD_PLACED", "merchant", "payment pe jhagda"), ("HOLD_LIFTED", "merchant", None)]


# ---------------------------------------------------------------- agent

async def test_place_hold_by_voice_after_haan(session, merchant):
    c = await _customer(session, merchant)
    orch = Orchestrator(FakeLLM([tool("set_hold", customer="Ramesh Patil", on_hold=True, reason="jhagda")]))
    r = await orch.handle_text(session, merchant.id, "Ramesh ka khata hold karo, jhagda hai", "s1", now=NOW)
    assert r.outcome == "awaiting_confirmation" and r.pending[0]["tool"] == "set_hold"
    await session.refresh(c)
    assert not c.on_hold
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    await session.refresh(c)
    assert c.on_hold and c.hold_reason == "jhagda"
    [ev] = await _events(session, c.id)
    assert ev.actor == "merchant"


async def test_lift_while_overdue_names_the_overdue_amount(session, merchant):
    c = await _customer(session, merchant, overdue_days=20)
    await ledger.apply_overdue_holds(session, TODAY)
    await session.commit()
    orch = Orchestrator(FakeLLM([tool("set_hold", customer="Ramesh Patil", on_hold=False)]))
    r = await orch.handle_text(session, merchant.id, "Ramesh ka hold hata do", "s1", now=NOW)
    assert "₹500" in r.pending[0]["summary"] and "₹500" in r.reply
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    await session.refresh(c)
    assert not c.on_hold


async def test_lift_without_overdue_names_no_amount(session, merchant):
    c = await _customer(session, merchant)
    await ledger.set_hold(session, merchant.id, c.id, True, "jhagda", "merchant", TODAY)
    await session.commit()
    r = await Orchestrator(FakeLLM([tool("set_hold", customer="Ramesh Patil", on_hold=False)])).handle_text(
        session, merchant.id, "Ramesh ka hold hata do", "s1", now=NOW)
    assert "₹" not in r.pending[0]["summary"]


async def test_udhaar_on_hold_refused_by_tool(session, merchant):
    c = await _customer(session, merchant, overdue_days=20)
    await ledger.apply_overdue_holds(session, TODAY)
    await session.commit()

    def speak(messages):
        err = last_results(messages)[0]["error"]
        assert "hold" in err.lower()
        return [text(err)]

    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=100)], speak)
                           ).handle_text(session, merchant.id, "Ramesh ko 100", "s1", now=NOW)
    assert not r.pending and "hold" in r.reply.lower()
    assert (await session.execute(select(PendingAction))).scalars().all() == []
    entries = (await session.execute(select(LedgerEntry).where(LedgerEntry.customer_id == c.id))).scalars().all()
    assert len(entries) == 1


async def test_payment_on_hold_still_works_by_voice(session, merchant):
    c = await _customer(session, merchant, overdue_days=20)
    await ledger.apply_overdue_holds(session, TODAY)
    await session.commit()
    orch = Orchestrator(FakeLLM([tool("record_payment", customer="Ramesh Patil", amount_rupees=200, mode="cash")]))
    await orch.handle_text(session, merchant.id, "Ramesh ne 200 diye", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"
    await session.refresh(c)
    assert c.balance_paise == 30000 and c.on_hold


async def test_get_balance_reports_hold(session, merchant):
    await _customer(session, merchant, overdue_days=20)
    await ledger.apply_overdue_holds(session, TODAY)
    await session.commit()

    def speak(messages):
        res = last_results(messages)[0]
        assert res["on_hold"] is True and "₹500" in res["hold_reason"]
        return [text("Ramesh hold par hai.")]

    await Orchestrator(FakeLLM([tool("get_balance", customer="Ramesh Patil")], speak)).handle_text(
        session, merchant.id, "Ramesh ka hisaab", "s1", now=NOW)


# ---------------------------------------------------------------- REST

async def test_api_shows_hold_and_refuses_manual_udhaar(client):
    import uuid

    from app.db import base
    from tests.api.conftest import create_customer, create_merchant

    mid = await create_merchant(client)
    cid = await create_customer(client, mid)
    async with base.get_sessionmaker()() as s:
        await ledger.set_hold(s, uuid.UUID(mid), uuid.UUID(cid), True, "jhagda", "merchant", date.today())
        await s.commit()
    detail = (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()
    assert detail["on_hold"] is True and detail["hold_reason"] == "jhagda"
    listed = (await client.get(f"/api/merchants/{mid}/customers")).json()["customers"][0]
    assert listed["on_hold"] is True
    r = await client.post(f"/api/merchants/{mid}/customers/{cid}/credit", json={"amount_rupees": 100})
    assert r.status_code == 400 and "hold" in r.json()["detail"].lower()
    r = await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 100})
    assert r.status_code == 201


async def test_api_merchant_grace_days(client):
    from tests.api.conftest import create_merchant

    mid = await create_merchant(client)
    assert (await client.get(f"/api/merchants/{mid}")).json()["hold_grace_days"] == 15
    r = await client.patch(f"/api/merchants/{mid}", json={"hold_grace_days": 7})
    assert r.status_code == 200 and r.json()["hold_grace_days"] == 7
    assert (await client.patch(f"/api/merchants/{mid}", json={"hold_grace_days": -1})).status_code == 422


async def test_scheduled_job_holds_and_announces(session, merchant, monkeypatch):
    from app.scheduler import jobs

    published = []
    monkeypatch.setattr(jobs.bus, "publish", lambda mid, kind, text, **data: published.append((kind, text, data)))
    today = datetime.now(get_settings().tz).date()
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil")
    await ledger.add_credit(session, merchant.id, c.id, 50000, due_date=today - timedelta(days=30))
    await session.commit()
    await jobs.daily_holds()
    await session.refresh(c)
    assert c.on_hold
    assert published == [("holds_placed", "1 customers ka khata hold par gaya (overdue).", {"customer_ids": [str(c.id)]})]
