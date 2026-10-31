"""Feature 1: credit limit by voice, with a near-limit warning (BUILD_CONTEXT.md §11.1)."""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.models import CustomerEvent, LedgerEntry, PendingAction, PendingStatus
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from tests.conftest import FakeLLM, last_results, text, tool

NOW = datetime(2026, 10, 29, 11, 0, tzinfo=get_settings().tz)


async def _ramesh(session, merchant, balance_paise: int = 0, limit_paise: int | None = None):
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil", "9822001234")
    if balance_paise:
        await ledger.add_credit(session, merchant.id, c.id, balance_paise)
    if limit_paise is not None:
        await ledger.set_credit_limit(session, merchant.id, c.id, limit_paise)
    await session.commit()
    return c


async def _events(session, customer_id):
    stmt = select(CustomerEvent).where(CustomerEvent.customer_id == customer_id).order_by(CustomerEvent.created_at)
    return list((await session.execute(stmt)).scalars())


async def _credits(session, customer_id):
    stmt = select(LedgerEntry).where(LedgerEntry.customer_id == customer_id)
    return list((await session.execute(stmt)).scalars())


# ---------------------------------------------------------------- service rules

async def test_limit_change_is_appended_as_event(session, merchant):
    c = await _ramesh(session, merchant)
    await ledger.set_credit_limit(session, merchant.id, c.id, 200000)
    await ledger.set_credit_limit(session, merchant.id, c.id, 300000)
    await session.commit()
    await session.refresh(c)
    assert c.credit_limit_paise == 300000
    events = await _events(session, c.id)
    assert [e.event_type for e in events] == ["CREDIT_LIMIT_SET", "CREDIT_LIMIT_SET"]
    assert events[0].payload == {"old_limit_paise": None, "new_limit_paise": 200000}
    assert events[1].payload == {"old_limit_paise": 200000, "new_limit_paise": 300000}
    assert {e.actor for e in events} == {"merchant"}


async def test_negative_limit_rejected(session, merchant):
    c = await _ramesh(session, merchant)
    with pytest.raises(LedgerError) as exc:
        await ledger.set_credit_limit(session, merchant.id, c.id, -100)
    assert exc.value.key == "err_limit_negative"
    assert await _events(session, c.id) == []


async def test_customer_events_are_append_only(session, merchant):
    cid = (await _ramesh(session, merchant, limit_paise=100000)).id
    ev = (await _events(session, cid))[0]
    ev.payload = {"tampered": True}
    with pytest.raises(LedgerError):
        await session.flush()
    await session.rollback()
    ev = (await _events(session, cid))[0]
    await session.delete(ev)
    with pytest.raises(LedgerError):
        await session.flush()


async def test_udhaar_over_limit_is_blocked(session, merchant):
    c = await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    with pytest.raises(LedgerError) as exc:
        await ledger.add_credit(session, merchant.id, c.id, 80000)
    assert exc.value.key == "err_over_limit"
    assert exc.value.params["over"] == "₹300"


async def test_udhaar_up_to_limit_is_allowed(session, merchant):
    c = await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    assert c.balance_paise == 200000


async def test_override_allows_only_the_approved_overage(session, merchant):
    c = await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    with pytest.raises(LedgerError):
        await ledger.add_credit(session, merchant.id, c.id, 80000, allow_over_limit_paise=20000)
    await ledger.add_credit(session, merchant.id, c.id, 80000, allow_over_limit_paise=30000)
    assert c.balance_paise == 230000
    override = [e for e in await _events(session, c.id) if e.event_type == "CREDIT_LIMIT_OVERRIDE"]
    assert len(override) == 1 and override[0].payload["over_by_paise"] == 30000


async def test_zero_limit_blocks_all_udhaar(session, merchant):
    c = await _ramesh(session, merchant, limit_paise=0)
    await ledger.record_payment(session, merchant.id, c.id, 50000)  # ₹500 advance
    for kwargs in ({}, {"allow_over_limit_paise": 10**9}):
        with pytest.raises(LedgerError) as exc:
            await ledger.add_credit(session, merchant.id, c.id, 100, **kwargs)
        assert exc.value.key == "err_limit_zero"


async def test_no_limit_means_no_block(session, merchant):
    c = await _ramesh(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 5_000_000)
    assert ledger.credit_status(c)["limit_paise"] is None


def test_credit_status_math():
    from types import SimpleNamespace

    c = SimpleNamespace(balance_paise=160000, credit_limit_paise=200000)
    s = ledger.credit_status(c)
    assert (s["used_pct"], s["near_limit"], s["over_by_paise"]) == (80, True, 0)
    s = ledger.credit_status(c, extra_paise=50000)
    assert (s["projected_paise"], s["over_by_paise"], s["near_limit"]) == (210000, 10000, False)
    s = ledger.credit_status(SimpleNamespace(balance_paise=150000, credit_limit_paise=200000))
    assert (s["used_pct"], s["near_limit"]) == (75, False)
    s = ledger.credit_status(SimpleNamespace(balance_paise=-5000, credit_limit_paise=200000))
    assert s["used_pct"] == 0


# ---------------------------------------------------------------- agent: set the limit by voice

async def test_set_limit_by_voice_writes_only_after_haan(session, merchant):
    c = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("set_credit_limit", customer="Ramesh Patil", limit_rupees=2000)]))
    r = await orch.handle_text(session, merchant.id, "Ramesh ki limit 2000 rakho", "s1", now=NOW)
    assert r.outcome == "awaiting_confirmation"
    assert r.pending[0]["tool"] == "set_credit_limit"
    assert "₹2,000" in r.pending[0]["summary"]
    await session.refresh(c)
    assert c.credit_limit_paise is None and await _events(session, c.id) == []

    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"
    await session.refresh(c)
    assert c.credit_limit_paise == 200000
    assert len(await _events(session, c.id)) == 1


async def test_negative_limit_by_voice_is_refused(session, merchant):
    await _ramesh(session, merchant)

    def speak(messages):
        assert "error" in last_results(messages)[0]
        return [text("Limit minus mein nahi ho sakti.")]

    r = await Orchestrator(FakeLLM([tool("set_credit_limit", customer="Ramesh Patil", limit_rupees=-500)], speak)
                           ).handle_text(session, merchant.id, "Ramesh ki limit minus 500", "s1", now=NOW)
    assert not r.pending


async def test_get_credit_status(session, merchant):
    await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)

    def speak(messages):
        res = last_results(messages)[0]
        assert res["balance"] == "₹1,500" and res["limit"] == "₹2,000" and res["percent_used"] == 75
        return [text("₹2,000 mein se ₹1,500 use hua.")]

    await Orchestrator(FakeLLM([tool("get_credit_status", customer="Ramesh Patil")], speak)).handle_text(
        session, merchant.id, "Ramesh ki limit kitni bachi", "s1", now=NOW)


# ---------------------------------------------------------------- agent: udhaar against the limit

async def test_near_limit_warning_on_confirmation_card(session, merchant):
    await _ramesh(session, merchant, balance_paise=100000, limit_paise=200000)
    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=600)])).handle_text(
        session, merchant.id, "Ramesh ko 600", "s1", now=NOW)
    card = r.pending[0]
    assert card["credit_warning"] == {"kind": "near_limit", "percent_used": 80, "limit": "₹2,000"}
    assert "80%" in card["summary"]
    assert not card["needs_double_confirmation"]


async def test_below_80_percent_has_no_warning(session, merchant):
    await _ramesh(session, merchant, balance_paise=100000, limit_paise=200000)
    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=500)])).handle_text(
        session, merchant.id, "Ramesh ko 500", "s1", now=NOW)
    assert r.pending[0]["credit_warning"] is None


async def test_over_limit_needs_separate_confirmation_naming_overage(session, merchant):
    c = await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=800)]))
    r = await orch.handle_text(session, merchant.id, "Ramesh ko 800", "s1", now=NOW)
    card = r.pending[0]
    assert card["credit_warning"] == {"kind": "over_limit", "over_by": "₹300", "limit": "₹2,000"}
    assert card["needs_double_confirmation"]
    assert "₹300" in r.reply  # the read-back names the overage

    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "awaiting_second_confirm"
    assert "₹300" in r.reply and "limit" in r.reply.lower()
    assert len(await _credits(session, c.id)) == 1  # still only the seed credit

    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"
    await session.refresh(c)
    assert c.balance_paise == 230000


async def test_over_limit_nahi_at_second_step_cancels(session, merchant):
    c = await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=800)]))
    await orch.handle_text(session, merchant.id, "Ramesh ko 800", "s1", now=NOW)
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "nahi", "s1", now=NOW)
    assert r.outcome == "cancelled"
    await session.refresh(c)
    assert c.balance_paise == 150000


async def test_over_limit_and_big_amount_need_three_yeses(session, merchant):
    c = await _ramesh(session, merchant, limit_paise=1_000_000)  # ₹10,000 limit, ₹10,000 big-amount threshold
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=12000)]))
    await orch.handle_text(session, merchant.id, "Ramesh ko 12000", "s1", now=NOW)
    r1 = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    r2 = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert (r1.outcome, r2.outcome) == ("awaiting_second_confirm", "awaiting_second_confirm")
    assert "₹2,000" in r2.reply  # the last step is the one that names the overage
    r3 = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r3.outcome == "committed"
    await session.refresh(c)
    assert c.balance_paise == 1_200_000


async def test_zero_limit_refused_by_tool_not_agent(session, merchant):
    c = await _ramesh(session, merchant, limit_paise=0)

    def speak(messages):
        err = last_results(messages)[0]["error"]
        assert "limit" in err.lower()
        return [text(err)]

    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=100)], speak)
                           ).handle_text(session, merchant.id, "Ramesh ko 100", "s1", now=NOW)
    assert not r.pending
    assert (await session.execute(select(PendingAction))).scalars().all() == []
    assert await _credits(session, c.id) == []


async def test_agent_cannot_skip_the_limit_check(session, merchant):
    """Arguments outside the schema (an 'override' the model invents) are dropped; the tool still decides."""
    await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=800,
                                         allow_over_limit_paise=10**9, override=True)])).handle_text(
        session, merchant.id, "Ramesh ko 800", "s1", now=NOW)
    pa = (await session.execute(select(PendingAction))).scalar_one()
    assert pa.args["over_limit_paise"] == 30000
    assert r.pending[0]["needs_double_confirmation"]


async def test_two_udhaars_in_one_turn_count_together(session, merchant):
    await _ramesh(session, merchant, balance_paise=100000, limit_paise=200000)
    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=600),
                                    tool("add_credit", customer="Ramesh Patil", amount_rupees=600)])).handle_text(
        session, merchant.id, "Ramesh ko 600 aur 600", "s1", now=NOW)
    assert r.pending[0]["credit_warning"]["kind"] == "near_limit"
    assert r.pending[1]["credit_warning"] == {"kind": "over_limit", "over_by": "₹200", "limit": "₹2,000"}


async def test_balance_grows_before_commit_then_commit_fails(session, merchant):
    """The service re-checks at commit: an overage larger than the one the merchant approved is refused."""
    c = await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=800)]))
    await orch.handle_text(session, merchant.id, "Ramesh ko 800", "s1", now=NOW)
    await ledger.add_credit(session, merchant.id, c.id, 10000)  # entered at the counter in the meantime
    await session.commit()
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "failed"
    await session.refresh(c)
    assert c.balance_paise == 160000
    assert (await session.execute(select(PendingAction))).scalar_one().status == PendingStatus.CANCELLED


# ---------------------------------------------------------------- REST

async def test_manual_credit_over_limit_is_refused(client):
    from tests.api.conftest import create_customer, create_merchant

    mid = await create_merchant(client)
    cid = await create_customer(client, mid)
    from app.db import base

    async with base.get_sessionmaker()() as s:
        import uuid

        await ledger.set_credit_limit(s, uuid.UUID(mid), uuid.UUID(cid), 50000)
        await s.commit()
    r = await client.post(f"/api/merchants/{mid}/customers/{cid}/credit", json={"amount_rupees": 600})
    assert r.status_code == 400 and "₹100" in r.json()["detail"]
    r = await client.get(f"/api/merchants/{mid}/customers/{cid}")
    assert r.json()["credit"] == {"limit": "₹500", "limit_paise": 50000, "percent_used": 0, "near_limit": False}


async def _over_limit_with_big_other(session, merchant, order):
    """Ramesh at ₹1,500 of ₹2,000 asks ₹800 (₹300 over); Sunita asks ₹15,000 (big amount). Same "haan"."""
    r = await _ramesh(session, merchant, balance_paise=150000, limit_paise=200000)
    await ledger.create_customer(session, merchant.id, "Sunita Devi")
    await session.commit()
    calls = {"ramesh": tool("add_credit", customer="Ramesh Patil", amount_rupees=800),
             "sunita": tool("add_credit", customer="Sunita Devi", amount_rupees=15000)}
    orch = Orchestrator(FakeLLM([calls[k] for k in order]))
    await orch.handle_text(session, merchant.id, "Ramesh ko 800 aur Sunita ko 15000", "s1", now=NOW)
    return r, orch


@pytest.mark.parametrize("order", [("sunita", "ramesh"), ("ramesh", "sunita")])
async def test_overage_gets_its_own_confirmation_when_waiting_with_another(session, merchant, order):
    """Regression (v2_gap_report.md risk rule 1): one "haan" must not satisfy two different second prompts."""
    r, orch = await _over_limit_with_big_other(session, merchant, order)
    replies = []
    for _ in range(2):
        out = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
        assert out.outcome == "awaiting_second_confirm"
        replies.append(out.reply)
    assert any("₹300" in x and "limit" in x.lower() for x in replies)  # the overage named in its own prompt
    assert any("₹15,000" in x for x in replies)                       # and so was the big amount
    await session.refresh(r)
    assert r.balance_paise == 150000
    out = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert out.outcome == "committed"
    await session.refresh(r)
    assert r.balance_paise == 230000
