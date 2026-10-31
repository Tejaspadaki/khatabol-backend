"""§20 Partial payments: payment_allocations, oldest-due FIFO, targeted allocation, status, advance, reversal."""

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.models import EntryType, LedgerEntry, PaymentAllocation
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from tests.conftest import FakeLLM, last_results, text, tool

NOW = datetime(2026, 10, 29, 11, 0, tzinfo=get_settings().tz)
TODAY = NOW.date()


async def _customer(session, merchant, name="Ramesh Patil"):
    c = await ledger.create_customer(session, merchant.id, name, "9822001234")
    await session.commit()
    return c


async def _status(session, customer_id):
    return {k: v for k, v in (await ledger.entry_statuses(session, customer_id)).items()}


async def _allocs(session):
    return list((await session.execute(select(PaymentAllocation).order_by(PaymentAllocation.created_at))).scalars())


# ---------------------------------------------------------------- FIFO by oldest due

async def test_payment_clears_oldest_due_first_not_oldest_created(session, merchant):
    c = await _customer(session, merchant)
    a = await ledger.add_credit(session, merchant.id, c.id, 10000, due_date=date(2026, 12, 1))
    b = await ledger.add_credit(session, merchant.id, c.id, 10000, due_date=date(2026, 11, 1))
    await ledger.record_payment(session, merchant.id, c.id, 10000)
    st = await _status(session, c.id)
    assert st[b.id] == ("PAID", 0) and st[a.id] == ("OPEN", 10000)


async def test_undated_udhaar_is_paid_after_dated(session, merchant):
    c = await _customer(session, merchant)
    undated = await ledger.add_credit(session, merchant.id, c.id, 10000)
    dated = await ledger.add_credit(session, merchant.id, c.id, 10000, due_date=date(2026, 11, 20))
    await ledger.record_payment(session, merchant.id, c.id, 15000)
    st = await _status(session, c.id)
    assert st[dated.id] == ("PAID", 0) and st[undated.id] == ("PARTIAL", 5000)


async def test_status_transitions_open_partial_paid(session, merchant):
    c = await _customer(session, merchant)
    e = await ledger.add_credit(session, merchant.id, c.id, 50000, due_date=date(2026, 11, 5))
    assert (await _status(session, c.id))[e.id] == ("OPEN", 50000)
    await ledger.record_payment(session, merchant.id, c.id, 30000)
    assert (await _status(session, c.id))[e.id] == ("PARTIAL", 20000)
    await ledger.record_payment(session, merchant.id, c.id, 20000)
    assert (await _status(session, c.id))[e.id] == ("PAID", 0)


# ---------------------------------------------------------------- targeted allocation

async def test_targeted_payment_goes_to_the_named_entry_first(session, merchant):
    c = await _customer(session, merchant)
    old = await ledger.add_credit(session, merchant.id, c.id, 30000, due_date=date(2026, 11, 1))
    diwali = await ledger.add_credit(session, merchant.id, c.id, 50000, due_date=date(2026, 11, 20), items="Diwali saamaan")
    await ledger.record_payment(session, merchant.id, c.id, 60000, target_entry_id=diwali.id)
    st = await _status(session, c.id)
    assert st[diwali.id] == ("PAID", 0) and st[old.id] == ("PARTIAL", 20000)  # the extra ₹100 went oldest-due first


async def test_target_must_be_an_open_udhaar_of_that_customer(session, merchant):
    c = await _customer(session, merchant)
    other = await _customer(session, merchant, "Sunita Devi")
    theirs = await ledger.add_credit(session, merchant.id, other.id, 10000)
    paid = await ledger.add_credit(session, merchant.id, c.id, 10000)
    await ledger.record_payment(session, merchant.id, c.id, 10000)
    for target in (theirs.id, paid.id):
        with pytest.raises(LedgerError):
            await ledger.record_payment(session, merchant.id, c.id, 5000, target_entry_id=target)


async def test_voice_targeted_payment(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 30000, due_date=date(2026, 11, 1), items="atta")
    diwali = await ledger.add_credit(session, merchant.id, c.id, 50000, due_date=date(2026, 11, 20), items="Diwali saamaan")
    await session.commit()
    orch = Orchestrator(FakeLLM([tool("record_payment", customer="Ramesh Patil", amount_rupees=300, mode="cash",
                                      for_entry="Diwali wala bill")]))
    r = await orch.handle_text(session, merchant.id, "Ramesh ne Diwali wale bill ka 300 diya", "s1", now=NOW)
    assert "Diwali" in r.pending[0]["summary"]
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert (await _status(session, c.id))[diwali.id] == ("PARTIAL", 20000)


async def test_voice_ambiguous_target_asks(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 30000, items="Diwali mithai")
    await ledger.add_credit(session, merchant.id, c.id, 50000, items="Diwali saamaan")
    await session.commit()

    def ask(messages):
        err = last_results(messages)[0]["error"]
        assert "mithai" in err and "saamaan" in err
        return [text("Kaunsa Diwali bill?")]

    r = await Orchestrator(FakeLLM([tool("record_payment", customer="Ramesh Patil", amount_rupees=100, mode="cash",
                                         for_entry="Diwali")], ask)).handle_text(
        session, merchant.id, "Ramesh ne Diwali ka 100 diya", "s1", now=NOW)
    assert not r.pending


# ---------------------------------------------------------------- advance and reversal

async def test_overpayment_advance_is_applied_to_next_udhaar(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 20000)
    await ledger.record_payment(session, merchant.id, c.id, 30000)
    assert c.balance_paise == -10000
    nxt = await ledger.add_credit(session, merchant.id, c.id, 25000)
    assert (await _status(session, c.id))[nxt.id] == ("PARTIAL", 15000)
    assert c.balance_paise == 15000


async def test_reversing_a_payment_reopens_entries_without_touching_rows(session, merchant):
    c = await _customer(session, merchant)
    e = await ledger.add_credit(session, merchant.id, c.id, 30000)
    p = await ledger.record_payment(session, merchant.id, c.id, 30000)
    before = [(a.id, a.amount_paise) for a in await _allocs(session)]
    await ledger.reverse_entry(session, merchant.id, p.id)
    after = await _allocs(session)
    assert [(a.id, a.amount_paise) for a in after[:len(before)]] == before  # original rows unchanged
    assert after[-1].amount_paise == -30000 and after[-1].reversal_entry_id is not None
    assert (await _status(session, c.id))[e.id] == ("OPEN", 30000)


async def test_reversing_a_payment_lets_an_advance_cover_the_reopened_entry(session, merchant):
    c = await _customer(session, merchant)
    e = await ledger.add_credit(session, merchant.id, c.id, 30000)
    p1 = await ledger.record_payment(session, merchant.id, c.id, 30000)
    await ledger.record_payment(session, merchant.id, c.id, 10000)  # ₹100 advance
    await ledger.reverse_entry(session, merchant.id, p1.id)
    assert (await _status(session, c.id))[e.id] == ("PARTIAL", 20000)
    assert c.balance_paise == 20000


async def test_reversing_a_credit_moves_its_payment_to_the_next_open_udhaar(session, merchant):
    c = await _customer(session, merchant)
    first = await ledger.add_credit(session, merchant.id, c.id, 30000, due_date=date(2026, 11, 1))
    second = await ledger.add_credit(session, merchant.id, c.id, 30000, due_date=date(2026, 11, 10))
    await ledger.record_payment(session, merchant.id, c.id, 30000)
    await ledger.reverse_entry(session, merchant.id, first.id)
    st = await _status(session, c.id)
    assert st[first.id][0] == "REVERSED" and st[second.id] == ("PAID", 0)


async def test_allocations_and_ledger_rows_are_append_only(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 30000)
    await ledger.record_payment(session, merchant.id, c.id, 10000)
    await session.commit()
    session.info.pop("allow_backdate", None)
    a = (await _allocs(session))[0]
    a.amount_paise = 1
    with pytest.raises(LedgerError):
        await session.flush()
    await session.rollback()
    e = (await session.execute(select(LedgerEntry).limit(1))).scalar_one()
    e.amount_paise = 1
    with pytest.raises(LedgerError):
        await session.flush()
    await session.rollback()
    e = (await session.execute(select(LedgerEntry).limit(1))).scalar_one()
    await session.delete(e)
    with pytest.raises(LedgerError):
        await session.flush()


# ---------------------------------------------------------------- waiver (ADJUSTMENT)

async def test_waiver_is_an_adjustment_with_reason(session, merchant):
    c = await _customer(session, merchant)
    e = await ledger.add_credit(session, merchant.id, c.id, 50000)
    adj = await ledger.record_adjustment(session, merchant.id, c.id, 5000, reason="50 chhod diye")
    assert adj.type == EntryType.ADJUSTMENT and adj.reason == "50 chhod diye"
    assert c.balance_paise == 45000
    assert (await _status(session, c.id))[e.id] == ("PARTIAL", 45000)
    s = await ledger.daily_summary(session, merchant.id, datetime.now(get_settings().tz).date())
    assert s["collected_paise"] == 0 and s["waived_paise"] == 5000
    with pytest.raises(LedgerError):
        await ledger.record_adjustment(session, merchant.id, c.id, 1000, reason="")
    with pytest.raises(LedgerError):  # can't waive more than is owed
        await ledger.record_adjustment(session, merchant.id, c.id, 50000, reason="sab maaf")
    assert ledger.derive_balance(await ledger.customer_entries(session, c.id)) == c.balance_paise


# ---------------------------------------------------------------- reminders + API

async def test_reminder_quotes_remaining_after_partial(session, merchant):
    from app.scheduler.reminders import send_reminders

    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    await ledger.record_payment(session, merchant.id, c.id, 30000)
    b = await ledger.get_balance(session, merchant.id, c.id, TODAY)
    [r] = await send_reminders(session, merchant, [(c, b["balance_paise"], None)])
    assert r.amount_paise == 20000 and "₹200" in r.message and "₹500" not in r.message


async def test_api_history_shows_status_and_remaining(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    mid = await create_merchant(client)
    cid = await create_customer(client, mid)
    await give_credit(client, mid, cid, 500)
    await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 300})
    entries = (await client.get(f"/api/merchants/{mid}/customers/{cid}/history")).json()["entries"]
    credit = next(e for e in entries if e["type"] == "CREDIT")
    assert credit["status"] == "PARTIAL" and credit["remaining"] == "₹200"
