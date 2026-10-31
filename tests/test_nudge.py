"""Feature 3: partial-payment nudge (BUILD_CONTEXT.md §11.3)."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.models import Reminder
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.scheduler.reminders import send_payment_nudge
from tests.conftest import FakeLLM, last_results, text, tool

NOW = datetime(2026, 10, 29, 11, 0, tzinfo=get_settings().tz)
TODAY = NOW.date()


async def _ramesh(session, merchant, owes=50000, paid=20000):
    """Ramesh owes ₹500, then pays ₹200: ₹300 left. Returns (customer, payment entry)."""
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil", "9822001234")
    await ledger.add_credit(session, merchant.id, c.id, owes, due_date=TODAY + timedelta(days=3))
    p = await ledger.record_payment(session, merchant.id, c.id, paid) if paid else None
    await session.commit()
    return c, p


async def _reminders(session):
    return list((await session.execute(select(Reminder))).scalars())


# ---------------------------------------------------------------- read-back after a payment

async def test_payment_readback_names_paid_and_still_due(session, merchant):
    await _ramesh(session, merchant, paid=0)
    orch = Orchestrator(FakeLLM([tool("record_payment", customer="Ramesh Patil", amount_rupees=200, mode="cash")]))
    await orch.handle_text(session, merchant.id, "Ramesh ne 200 diye", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.reply.startswith("Ho gaya. Ramesh Patil se ₹200 mile, ab ₹300 baaki.")
    assert "reminder" in r.reply.lower()  # offers the nudge
    nudge = r.committed[0]["nudge"]
    assert nudge["remaining"] == "₹300" and nudge["payment_id"] == r.committed[0]["entry"]["entry_id"]
    assert await _reminders(session) == []  # offered, not sent


async def test_full_payment_offers_no_nudge(session, merchant):
    await _ramesh(session, merchant, paid=0)
    orch = Orchestrator(FakeLLM([tool("record_payment", customer="Ramesh Patil", amount_rupees=500, mode="cash")]))
    await orch.handle_text(session, merchant.id, "Ramesh ne 500 diye", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.committed[0]["nudge"] is None and "reminder" not in r.reply.lower()


async def test_no_offer_without_shop_upi(session, merchant):
    merchant.upi_vpa = None
    await _ramesh(session, merchant, paid=0)
    orch = Orchestrator(FakeLLM([tool("record_payment", customer="Ramesh Patil", amount_rupees=200, mode="cash")]))
    await orch.handle_text(session, merchant.id, "Ramesh ne 200 diye", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.committed[0]["nudge"] is None


# ---------------------------------------------------------------- sending the nudge

async def test_nudge_sent_only_after_haan(session, merchant):
    c, p = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil", payment_id=str(p.id))]))
    r = await orch.handle_text(session, merchant.id, "baaki ka reminder bhej do", "s1", now=NOW)
    assert r.outcome == "awaiting_confirmation" and "₹300" in r.pending[0]["summary"]
    assert await _reminders(session) == []
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"
    [rem] = await _reminders(session)
    assert rem.amount_paise == 30000 and rem.source_payment_id == p.id and rem.nudge_date == TODAY
    assert "₹300" in rem.message and "am=300.00" in rem.payment_link  # existing template + UPI link


async def test_amount_is_read_at_send_time(session, merchant):
    c, p = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil", payment_id=str(p.id))]))
    await orch.handle_text(session, merchant.id, "baaki ka reminder bhej do", "s1", now=NOW)
    await ledger.record_payment(session, merchant.id, c.id, 10000)  # ₹100 more at the counter before "haan"
    await session.commit()
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    [rem] = await _reminders(session)
    assert rem.amount_paise == 20000 and "₹200" in r.reply


async def test_balance_cleared_before_send_fails(session, merchant):
    c, p = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil", payment_id=str(p.id))]))
    await orch.handle_text(session, merchant.id, "baaki ka reminder bhej do", "s1", now=NOW)
    await ledger.record_payment(session, merchant.id, c.id, 30000)
    await session.commit()
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "failed" and await _reminders(session) == []


async def test_one_nudge_per_payment_per_day(session, merchant):
    c, p = await _ramesh(session, merchant)
    await send_payment_nudge(session, merchant, c, p.id, TODAY)
    await session.commit()

    def speak(messages):
        assert "aaj" in last_results(messages)[0]["error"].lower()
        return [text("Aaj is payment ka reminder ja chuka hai.")]

    r = await Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil", payment_id=str(p.id))],
                                   speak)).handle_text(session, merchant.id, "reminder bhejo", "s1", now=NOW)
    assert not r.pending
    with pytest.raises(LedgerError) as exc:  # the unique key holds even if the tool check is skipped
        await send_payment_nudge(session, merchant, c, p.id, TODAY)
    assert exc.value.key == "err_nudge_sent_today"
    # Only the savepoint was rolled back: the same transaction carries on (the gate relies on this).
    await send_payment_nudge(session, merchant, c, p.id, TODAY + timedelta(days=1))  # next day is fine
    await session.commit()
    assert len(await _reminders(session)) == 2


async def test_payment_id_defaults_to_latest_payment(session, merchant):
    c, p = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil")]))
    await orch.handle_text(session, merchant.id, "baaki ka reminder bhej do", "s1", now=NOW)
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    [rem] = await _reminders(session)
    assert rem.source_payment_id == p.id


async def test_payment_id_must_be_a_live_payment_of_that_customer(session, merchant):
    c, p = await _ramesh(session, merchant)
    credit = await ledger.add_credit(session, merchant.id, c.id, 1000)
    other = await ledger.create_customer(session, merchant.id, "Sunita Devi")
    await ledger.add_credit(session, merchant.id, other.id, 5000)
    other_pay = await ledger.record_payment(session, merchant.id, other.id, 1000)
    await session.commit()
    errors = []

    def collect(messages):
        errors.append(last_results(messages)[0].get("error"))
        return [text("nahi")]

    for pid in (credit.id, other_pay.id):
        await Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil", payment_id=str(pid))],
                                   collect)).handle_text(session, merchant.id, "reminder", "s1", now=NOW)
    await ledger.reverse_entry(session, merchant.id, p.id)
    await session.commit()
    await Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil", payment_id=str(p.id))],
                               collect)).handle_text(session, merchant.id, "reminder", "s1", now=NOW)
    assert len(errors) == 3 and all(errors)


async def test_nudge_needs_shop_upi(session, merchant):
    c, p = await _ramesh(session, merchant)
    merchant.upi_vpa = None
    await session.commit()

    def speak(messages):
        assert "UPI" in last_results(messages)[0]["error"]
        return [text("UPI ID set kijiye.")]

    r = await Orchestrator(FakeLLM([tool("send_partial_nudge", customer="Ramesh Patil")], speak)).handle_text(
        session, merchant.id, "reminder", "s1", now=NOW)
    assert not r.pending


# ---------------------------------------------------------------- one-tap from the app

async def test_api_one_tap_nudge_goes_through_the_gate(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    mid = await create_merchant(client)
    cid = await create_customer(client, mid, phone="9822001234")
    await give_credit(client, mid, cid, 500)
    pay = (await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 200})).json()
    body = {"session_id": "tap1", "tool": "send_partial_nudge",
            "args": {"customer": cid, "payment_id": pay["entry"]["entry_id"]}}
    r = await client.post(f"/api/merchants/{mid}/agent/propose", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["outcome"] == "awaiting_confirmation" and "₹300" in out["reply"]
    assert (await client.get(f"/api/merchants/{mid}/customers/{cid}/history")).status_code == 200

    r = await client.post(f"/api/merchants/{mid}/agent/message", json={"text": "haan", "session_id": "tap1"})
    assert r.json()["outcome"] == "committed"
    again = (await client.post(f"/api/merchants/{mid}/agent/propose", json=body)).json()
    assert again["outcome"] == "write_failed" and not again["pending"]


async def test_api_propose_only_allows_the_nudge(client):
    from tests.api.conftest import create_merchant

    mid = await create_merchant(client)
    r = await client.post(f"/api/merchants/{mid}/agent/propose",
                          json={"tool": "add_credit", "args": {"customer": "x", "amount_rupees": 1}})
    assert r.status_code == 422
