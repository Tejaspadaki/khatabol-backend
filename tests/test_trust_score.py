"""Feature 4: Trust Score + UPI Udhaar Check (plan.md §22, BUILD_CONTEXT.md §11.4)."""

import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.models import PendingAction, UpiTransaction, UpiTxnStatus
from app.ledger import service as ledger
from app.risk import trust_score
from tests.conftest import FakeLLM, last_results, text, tool

TZ = get_settings().tz
NOW = datetime(2026, 10, 29, 11, 0, tzinfo=TZ)
TODAY = NOW.date()


def _at(d: date) -> datetime:
    return datetime.combine(d, time(12, 0), tzinfo=TZ)


async def _history(session, merchant, name, rows, phone=None):
    """rows: (given_days_ago, amount_rupees, due_days_after_given | None, paid_days_after_given | None)."""
    c = await ledger.create_customer(session, merchant.id, name, phone)
    c.created_at = _at(TODAY - timedelta(days=max(r[0] for r in rows)))
    for ago, rupees, due_after, paid_after in sorted(rows, key=lambda r: -r[0]):
        given = TODAY - timedelta(days=ago)
        due = given + timedelta(days=due_after) if due_after is not None else None
        e = await ledger.add_credit(session, merchant.id, c.id, rupees * 100, due_date=due)
        e.created_at = _at(given)
        if paid_after is not None:
            p = await ledger.record_payment(session, merchant.id, c.id, rupees * 100)
            p.created_at = _at(given + timedelta(days=paid_after))
    await session.commit()
    return c


async def _upi(session, merchant, vpa, n, months, payer_name="RAHUL VERMA", amount=5000, customer=None):
    for i in range(n):
        session.add(UpiTransaction(
            merchant_id=merchant.id, txn_id=f"T-{vpa}-{i}", payer_vpa=vpa, payer_name=payer_name,
            amount_paise=amount, status=UpiTxnStatus.SALE, customer_id=customer.id if customer else None,
            paid_at=_at(TODAY - timedelta(days=int(months * 30 * i / max(n - 1, 1))))))
    await session.commit()


async def _good(session, merchant):
    """Four ₹500 udhaars over ~7 months, each repaid before its due date."""
    return await _history(session, merchant, "Ramesh Patil",
                          [(210, 500, 10, 5), (150, 500, 10, 7), (90, 500, 10, 3), (30, 500, 10, 9)])


async def _late(session, merchant):
    """Suresh: two ₹1,000 udhaars repaid late, and ₹1,800 still open and overdue."""
    return await _history(session, merchant, "Suresh Kumar",
                          [(120, 1000, 7, 30), (80, 1000, 7, 40), (38, 1800, 7, None)])


# ---------------------------------------------------------------- scoring rules

async def test_no_history_starts_small(session, merchant):
    a = await trust_score.assess(session, merchant, TODAY, vpa="rahul99@ybl")
    assert a.level == "new" and a.suggested_paise == 30000
    assert a.advice == "Naya customer hai. ₹300 tak se shuru karein."


async def test_good_payer_is_high(session, merchant):
    c = await _good(session, merchant)
    a = await trust_score.assess(session, merchant, TODAY, customer=c)
    assert a.level == "high"
    assert a.suggested_paise == 100000  # 2 × the largest udhaar repaid in full
    assert a.advice == "Trust Score: High. ₹1,000 tak de sakte hain."
    assert any("4 mein se 4" in r for r in a.reasons)


async def test_late_payer_is_low(session, merchant):
    c = await _late(session, merchant)
    a = await trust_score.assess(session, merchant, TODAY, customer=c)
    assert a.level == "low"
    assert a.suggested_paise == 50000  # half the usual udhaar
    assert any("late" in r for r in a.reasons) and any("overdue" in r for r in a.reasons)


async def test_partial_payment_by_due_date_counts_half(session, merchant):
    c = await ledger.create_customer(session, merchant.id, "Mohan Lal")
    given = TODAY - timedelta(days=60)
    e = await ledger.add_credit(session, merchant.id, c.id, 100000, due_date=given + timedelta(days=10))
    e.created_at = _at(given)
    p1 = await ledger.record_payment(session, merchant.id, c.id, 60000)
    p1.created_at = _at(given + timedelta(days=5))   # more than half, on time
    p2 = await ledger.record_payment(session, merchant.id, c.id, 40000)
    p2.created_at = _at(given + timedelta(days=20))  # the rest, late
    await session.commit()
    a = await trust_score.assess(session, merchant, TODAY, customer=c)
    assert a.signals["counted"] == 1 and a.signals["on_time"] == 0.5


async def test_udhaar_without_due_date_judged_against_30_days(session, merchant):
    c = await _history(session, merchant, "Kavita", [(100, 400, None, 20), (60, 400, None, 45)])
    a = await trust_score.assess(session, merchant, TODAY, customer=c)
    assert (a.signals["counted"], a.signals["on_time"]) == (2, 1)


async def test_suggestion_never_exceeds_room_under_the_limit(session, merchant):
    c = await _good(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    await ledger.set_credit_limit(session, merchant.id, c.id, 80000)
    await session.commit()
    a = await trust_score.assess(session, merchant, TODAY, customer=c)
    assert a.suggested_paise == 30000  # ₹800 limit − ₹500 owed


async def test_score_uses_behaviour_not_identity(session, merchant):
    a1 = await trust_score.assess(session, merchant, TODAY, customer=await _good(session, merchant))
    other = await _history(session, merchant, "Abdul Rehman",
                           [(210, 500, 10, 5), (150, 500, 10, 7), (90, 500, 10, 3), (30, 500, 10, 9)],
                           phone="9000000000")
    a2 = await trust_score.assess(session, merchant, TODAY, customer=other)
    assert (a1.level, a1.suggested_paise, a1.signals) == (a2.level, a2.suggested_paise, a2.signals)


# ---------------------------------------------------------------- UPI ID check (levels 1, 3, 4)

async def test_vpa_linked_to_customer_uses_their_khata(session, merchant):
    c = await _good(session, merchant)
    await ledger.link_upi(session, merchant.id, c.id, "ramesh@ybl")
    await session.commit()
    a = await trust_score.assess(session, merchant, TODAY, vpa="ramesh@ybl")
    assert a.customer_id == c.id and a.level == "high"
    assert "this_shop" in a.checked and "shops" not in a.checked  # level 2 needs §23 shops


async def test_unknown_vpa_with_long_payment_relationship(session, merchant):
    await _upi(session, merchant, "rahul99@ybl", n=12, months=8)
    a = await trust_score.assess(session, merchant, TODAY, vpa="rahul99@ybl")
    assert a.level == "medium" and a.suggested_paise == 100000
    assert any("UPI payment" in r for r in a.reasons)
    assert "upi_relationship" in a.checked


async def test_name_mismatch_warns_and_lowers(session, merchant):
    await _upi(session, merchant, "rahul99@ybl", n=12, months=8, payer_name="RAHUL VERMA")
    ok = await trust_score.assess(session, merchant, TODAY, vpa="rahul99@ybl", given_name="Rahul Verma")
    bad = await trust_score.assess(session, merchant, TODAY, vpa="rahul99@ybl", given_name="Sanjay Gupta")
    assert ok.level == "medium" and any("match" in r for r in ok.reasons)
    assert bad.level == "low" and any("RAHUL VERMA" in r and "Sanjay Gupta" in r for r in bad.reasons)
    assert "name_check" in bad.checked


# ---------------------------------------------------------------- agent: check_udhaar_risk

async def test_check_tool_by_spoken_vpa(session, merchant):
    def speak(messages):
        res = last_results(messages)[0]
        assert res["level"] == "new" and res["suggested_limit"] == "₹300" and res["amount_ok"] is False
        return [text(res["advice"])]

    r = await Orchestrator(FakeLLM([tool("check_udhaar_risk", vpa="Rahul99 @ YBL", amount_rupees=1000)], speak)
                           ).handle_text(session, merchant.id, "rahul99@ybl ko 1000 udhaar dena hai?", "s1", now=NOW)
    assert r.reply == "Naya customer hai. ₹300 tak se shuru karein." and not r.pending


async def test_check_tool_by_recent_payment(session, merchant):
    await _upi(session, merchant, "rahul99@ybl", n=12, months=8)
    session.add(UpiTransaction(merchant_id=merchant.id, txn_id="NOW-50", payer_vpa="rahul99@ybl", amount_paise=5000,
                               status=UpiTxnStatus.SALE, paid_at=NOW - timedelta(minutes=10)))
    await session.commit()

    def speak(messages):
        res = last_results(messages)[0]
        assert res["vpa"] == "rahul99@ybl" and res["level"] == "medium"
        return [text(res["advice"])]

    await Orchestrator(FakeLLM([tool("check_udhaar_risk", recent_payment_rupees=50)], speak)).handle_text(
        session, merchant.id, "jo abhi 50 aaye the unko udhaar du?", "s1", now=NOW)


async def test_check_tool_needs_a_target(session, merchant):
    def speak(messages):
        assert "error" in last_results(messages)[0]
        return [text("Kiska check karun?")]

    await Orchestrator(FakeLLM([tool("check_udhaar_risk")], speak)).handle_text(
        session, merchant.id, "udhaar du?", "s1", now=NOW)


# ---------------------------------------------------------------- Udhaar Guard (advice, never a block)

async def test_udhaar_guard_warns_low_customer_but_does_not_block(session, merchant):
    c = await _late(session, merchant)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Suresh Kumar", amount_rupees=1000)]))
    r = await orch.handle_text(session, merchant.id, "Suresh ko 1,000 ka saamaan", "s1", now=NOW)
    card = r.pending[0]
    assert card["trust_score"] == {"level": "low", "suggested_limit": "₹500", "warn": True}
    assert "Trust Score: Low" in card["summary"] and "₹500" in r.reply
    assert not card["needs_double_confirmation"]  # advice only: the usual single "haan"
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"
    await session.refresh(c)
    assert c.balance_paise == 280000


async def test_udhaar_guard_silent_within_suggestion_or_for_good_payers(session, merchant):
    await _late(session, merchant)
    await _good(session, merchant)
    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Suresh Kumar", amount_rupees=400),
                                    tool("add_credit", customer="Ramesh Patil", amount_rupees=900)])).handle_text(
        session, merchant.id, "Suresh ko 400, Ramesh ko 900", "s1", now=NOW)
    assert [p["trust_score"]["warn"] for p in r.pending] == [False, False]
    assert all("Trust Score" not in p["summary"] for p in r.pending)


async def test_score_never_changes_limit_or_hold(session, merchant):
    c = await _late(session, merchant)
    await trust_score.assess(session, merchant, TODAY, customer=c)
    await Orchestrator(FakeLLM([tool("check_udhaar_risk", customer="Suresh Kumar")], lambda m: [text("ok")])
                       ).handle_text(session, merchant.id, "Suresh ka trust_score?", "s1", now=NOW)
    await session.refresh(c)
    assert c.credit_limit_paise is None and c.on_hold is False
    assert (await session.execute(select(PendingAction))).scalars().all() == []


def test_only_the_tool_layer_and_api_read_the_score():
    root = Path(__file__).resolve().parents[1] / "app"
    readers = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.py")
                     if "app.risk" in p.read_text(encoding="utf-8") and "risk" not in p.parts)
    assert readers == ["agent/tools.py", "api/customers.py", "api/risk.py"]


# ---------------------------------------------------------------- REST

async def test_api_risk_check_and_customer_badge(client):
    from tests.api.conftest import create_customer, create_merchant

    mid = await create_merchant(client)
    cid = await create_customer(client, mid)
    r = await client.get(f"/api/merchants/{mid}/risk", params={"vpa": "new@ybl", "amount_rupees": 200})
    assert r.status_code == 200
    body = r.json()
    assert body["level"] == "new" and body["amount_ok"] is True and body["suggested_limit_paise"] == 30000
    assert (await client.get(f"/api/merchants/{mid}/risk")).status_code == 400
    detail = (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()
    assert detail["trust_score"]["level"] == "new" and detail["trust_score"]["reasons"]
    assert (await client.get(f"/api/merchants/{mid}/risk", params={"customer_id": str(uuid.uuid4())})).status_code == 404
