from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.models import EntrySource, PaymentMode
from app.ledger import service as ledger
from app.ledger.errors import LedgerError

TODAY = date(2026, 10, 29)


async def _customer(session, merchant, name="Ramesh Patil"):
    return await ledger.create_customer(session, merchant.id, name, "9822001234")


async def test_credit_payment_balance(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 50000, TODAY + timedelta(days=5))
    await ledger.record_payment(session, merchant.id, c.id, 20000, PaymentMode.CASH)
    b = await ledger.get_balance(session, merchant.id, c.id, TODAY)
    assert b["balance_paise"] == 30000
    assert b["next_due_date"] == TODAY + timedelta(days=5)
    assert b["next_due_paise"] == 30000
    assert b["last_payment"].amount_paise == 20000


async def test_rejects_non_positive_amount(session, merchant):
    c = await _customer(session, merchant)
    with pytest.raises(LedgerError):
        await ledger.add_credit(session, merchant.id, c.id, 0)


async def test_fifo_clears_oldest_credit_first(session, merchant):
    c = await _customer(session, merchant)
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    e1 = await ledger.add_credit(session, merchant.id, c.id, 30000, date(2026, 10, 20))
    e1.created_at = t0
    e2 = await ledger.add_credit(session, merchant.id, c.id, 20000, date(2026, 11, 5))
    e2.created_at = t0 + timedelta(days=1)
    await session.flush()
    b = await ledger.get_balance(session, merchant.id, c.id, TODAY)
    assert b["overdue_paise"] == 30000

    await ledger.record_payment(session, merchant.id, c.id, 35000)
    b = await ledger.get_balance(session, merchant.id, c.id, TODAY)
    assert b["balance_paise"] == 15000
    assert b["overdue_paise"] == 0  # the oldest (overdue) credit was cleared first
    assert b["next_due_date"] == date(2026, 11, 5)
    assert b["next_due_paise"] == 15000


async def test_overpayment_becomes_advance(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 20000)
    await ledger.record_payment(session, merchant.id, c.id, 30000, PaymentMode.UPI)
    b = await ledger.get_balance(session, merchant.id, c.id, TODAY)
    assert b["balance_paise"] == -10000


async def test_reverse_and_undo_last(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    pay = await ledger.record_payment(session, merchant.id, c.id, 10000)
    last = await ledger.last_reversible_entry(session, merchant.id)
    assert last.id == pay.id
    await ledger.reverse_entry(session, merchant.id, pay.id)
    assert c.balance_paise == 50000
    with pytest.raises(LedgerError):
        await ledger.reverse_entry(session, merchant.id, pay.id)
    # the reversed payment is skipped; next undo targets the credit
    assert (await ledger.last_reversible_entry(session, merchant.id)).type.value == "CREDIT"


async def test_verify_balances_fixes_drift(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    c.balance_paise = 1  # simulate drift
    fixed = await ledger.verify_balances(session, merchant.id)
    assert fixed and c.balance_paise == 50000


async def test_list_dues_filters(session, merchant):
    a = await _customer(session, merchant, "Overdue Wala")
    b = await _customer(session, merchant, "Aaj Wala")
    d = await _customer(session, merchant, "Next Month")
    await ledger.add_credit(session, merchant.id, a.id, 10000, TODAY - timedelta(days=3))
    await ledger.add_credit(session, merchant.id, b.id, 20000, TODAY)
    await ledger.add_credit(session, merchant.id, d.id, 30000, TODAY + timedelta(days=30))
    names = lambda rows: {r["customer"].name for r in rows}  # noqa: E731
    assert names(await ledger.list_dues(session, merchant.id, "overdue", TODAY)) == {"Overdue Wala"}
    assert names(await ledger.list_dues(session, merchant.id, "today", TODAY)) == {"Overdue Wala", "Aaj Wala"}
    assert names(await ledger.list_dues(session, merchant.id, "all", TODAY)) == {"Overdue Wala", "Aaj Wala", "Next Month"}


async def test_learns_nickname(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 10000, spoken_name="Ramu bhaiya")
    assert "ramu" in c.nicknames
    found = await ledger.find_customers(session, merchant.id, "ramu")
    assert found["decision"] == "match" and found["matches"][0]["customer"].id == c.id


async def test_daily_summary(session, merchant):
    c = await _customer(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    await ledger.record_payment(session, merchant.id, c.id, 20000, PaymentMode.UPI, EntrySource.UPI_AUTO)
    s = await ledger.daily_summary(session, merchant.id, datetime.now(get_settings().tz).date())
    assert s["credit_given_paise"] == 50000
    assert s["collected_upi_paise"] == 20000
    assert s["outstanding_paise"] == 30000
