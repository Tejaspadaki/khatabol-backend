"""Agent flow with a scripted LLM: the confirmation gate, read-back guard, name resolution, call budget."""

from datetime import datetime

from sqlalchemy import select

from app.agent.llm import LLMUnavailable
from app.agent.orchestrator import LLM_BUSY_REPLY, Orchestrator
from app.config import get_settings
from app.db.models import LedgerEntry, PendingAction, PendingStatus, VoiceLog
from app.ledger import service as ledger
from tests.conftest import FakeLLM, last_results, text, tool

NOW = datetime(2026, 10, 29, 11, 0, tzinfo=get_settings().tz)  # Thursday


async def _ramesh(session, merchant):
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil", "9822001234")
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    await session.commit()
    return c


async def _entries(session):
    return list((await session.execute(select(LedgerEntry))).scalars())


async def test_add_credit_by_name_needs_haan(session, merchant):
    c = await _ramesh(session, merchant)
    llm = FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=350, due="somvar")])
    orch = Orchestrator(llm)
    r = await orch.handle_text(session, merchant.id, "Ramesh Patil ko saadhe teen sau ka saamaan, somvar tak",
                               "s1", now=NOW)
    assert r.outcome == "awaiting_confirmation"
    assert r.pending[0]["summary"] == "Ramesh Patil ko ₹350 udhaar, somvar 2 November tak"
    assert r.reply == "Ramesh Patil ko ₹350 udhaar, somvar 2 November tak. Save karun?"
    assert len(llm.calls) == 1  # write-only round: no second LLM call for the read-back
    assert len(await _entries(session)) == 1  # only the seed credit: nothing written yet
    assert "(amounts normalized: Ramesh Patil ko 350 ka saamaan, somvar tak)" in llm.calls[0][0]["content"]

    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed" and r.reply == "Ho gaya. Ramesh Patil: ab ₹850 baaki."
    assert len(llm.calls) == 1  # "haan" never reaches the LLM
    await session.refresh(c)
    assert c.balance_paise == 85000
    new = [e for e in await _entries(session) if e.amount_paise == 35000][0]
    assert str(new.due_date) == "2026-11-02" and new.voice_log_id is not None


async def test_find_then_id_path_still_works(session, merchant):
    c = await _ramesh(session, merchant)

    def credit(messages):
        return [tool("add_credit", customer=last_results(messages)[0]["matches"][0]["customer_id"], amount_rupees=100)]

    orch = Orchestrator(FakeLLM([tool("find_customer", name_query="Ramesh")], credit))
    r = await orch.handle_text(session, merchant.id, "Ramesh ko 100", "s1", now=NOW)
    assert r.pending[0]["summary"] == "Ramesh Patil ko ₹100 udhaar"
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    await session.refresh(c)
    assert c.balance_paise == 60000


async def test_name_with_honorific_resolves(session, merchant):
    c = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("record_payment", customer="Ramesh Patil bhaiya", amount_rupees=100, mode="cash")]))
    await orch.handle_text(session, merchant.id, "Ramesh Patil bhaiya ne 100 diye", "s1", now=NOW)
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    await session.refresh(c)
    assert c.balance_paise == 40000  # "bhaiya" is an honorific, so nothing new to learn here
    assert c.nicknames == []


async def test_ambiguous_name_asks_instead_of_guessing(session, merchant):
    await ledger.create_customer(session, merchant.id, "Ramesh Patil", "9822001234")
    await ledger.create_customer(session, merchant.id, "Ramesh Sharma", "9822005678")
    await session.commit()

    def ask(messages):
        err = last_results(messages)[0]["error"]
        assert "kai customer" in err and "Ramesh Patil" in err and "..1234" in err and "Ramesh Sharma" in err
        return [text("Ramesh Patil ya Ramesh Sharma?")]

    r = await Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh", amount_rupees=100)], ask)).handle_text(
        session, merchant.id, "Ramesh ko 100", "s1", now=NOW)
    assert r.reply == "Ramesh Patil ya Ramesh Sharma?" and not r.pending and r.outcome == "write_failed"


async def test_unknown_name_offers_new_customer(session, merchant):
    def ask(messages):
        assert "Naya customer banaun, Kavita naam se?" in last_results(messages)[0]["error"]
        return [text("Naya customer banaun, Kavita naam se?")]

    r = await Orchestrator(FakeLLM([tool("get_balance", customer="Kavita")], ask)).handle_text(
        session, merchant.id, "Kavita ka hisaab", "s1", now=NOW)
    assert r.reply == "Naya customer banaun, Kavita naam se?"


async def test_nahi_cancels(session, merchant):
    c = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=500)]))
    await orch.handle_text(session, merchant.id, "Ramesh Patil ko 500", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "nahi", "s1", now=NOW)
    assert r.outcome == "cancelled"
    await session.refresh(c)
    assert c.balance_paise == 50000
    statuses = {p.status for p in (await session.execute(select(PendingAction))).scalars()}
    assert statuses == {PendingStatus.CANCELLED}


async def test_readback_guard_replaces_wrong_numbers(session, merchant):
    await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM(
        [tool("add_credit", customer="Ramesh Patil", amount_rupees=350), tool("get_balance", customer="Ramesh Patil")],
        [text("Ramesh ko ₹530 likh diya. Save karun?")],  # mixed round -> LLM writes the reply, and misspeaks
    ))
    r = await orch.handle_text(session, merchant.id, "Ramesh Patil ko 350", "s1", now=NOW)
    assert r.reply == "Ramesh Patil ko ₹350 udhaar. Save karun?"


async def test_large_amount_needs_double_confirm(session, merchant):
    c = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=15000)]))
    await orch.handle_text(session, merchant.id, "Ramesh Patil ko pandrah hazaar", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "awaiting_second_confirm"
    r = await orch.handle_text(session, merchant.id, "haan pakka", "s1", now=NOW)
    assert r.outcome == "committed"
    await session.refresh(c)
    assert c.balance_paise == 50000 + 1_500_000


async def test_multi_intent_in_one_call(session, merchant):
    await _ramesh(session, merchant)
    s = await ledger.create_customer(session, merchant.id, "Sunita Devi")
    await ledger.add_credit(session, merchant.id, s.id, 45000)
    await session.commit()
    llm = FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=500),
                   tool("record_payment", customer="Sunita", amount_rupees=200, mode="cash")])
    r = await Orchestrator(llm).handle_text(session, merchant.id, "Ramesh ko 500, Sunita ne 200 lautaye", "s1", now=NOW)
    assert [p["tool"] for p in r.pending] == ["add_credit", "record_payment"] and len(llm.calls) == 1
    assert r.reply == "Ramesh Patil ko ₹500 udhaar. Sunita Devi se ₹200 cash mile. Save karun?"


async def test_new_customer_and_credit_in_one_turn(session, merchant):
    def credit_new(messages):
        placeholder = last_results(messages)[0]["placeholder_customer_id"]
        return [tool("add_credit", customer=placeholder, amount_rupees=200, due="kal")]

    llm = FakeLLM([tool("create_customer", name="Kavita", phone="9000011111")], credit_new)
    orch = Orchestrator(llm)
    r = await orch.handle_text(session, merchant.id, "haan Kavita naam se bana do, 200 udhaar kal tak", "s1", now=NOW)
    assert [p["tool"] for p in r.pending] == ["create_customer", "add_credit"]
    assert len(llm.calls) == 2  # create_customer does not end the loop: its placeholder is needed
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"
    found = await ledger.find_customers(session, merchant.id, "kavita")
    assert found["matches"][0]["customer"].balance_paise == 20000


async def test_correction_cancels_and_reproposes(session, merchant):
    c = await _ramesh(session, merchant)

    def cancel_and_redo(messages):
        content = messages[-1]["content"]
        action_id = content.split("Still waiting for confirmation: ")[1].split(" = ")[0]
        return [tool("cancel_pending", action_id=action_id),
                tool("add_credit", customer="Ramesh Patil", amount_rupees=600)]

    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=500)], cancel_and_redo))
    await orch.handle_text(session, merchant.id, "Ramesh Patil ko 500", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "500 nahi 600", "s1", now=NOW)
    assert [p["summary"] for p in r.pending] == ["Ramesh Patil ko ₹600 udhaar"]
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    await session.refresh(c)
    assert c.balance_paise == 50000 + 60000


async def test_tool_error_is_returned_to_llm(session, merchant):
    await _ramesh(session, merchant)

    def ask(messages):
        assert "error" in last_results(messages)[0]
        return [text("Kab tak dega, tareekh bataiye?")]

    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=100, due="jab paise honge")],
                                ask))
    r = await orch.handle_text(session, merchant.id, "Ramesh Patil ko 100, jab paise honge", "s1", now=NOW)
    assert r.reply == "Kab tak dega, tareekh bataiye?" and not r.pending
    assert r.tool_calls[0]["ok"] is False


async def test_balance_question_is_read_only(session, merchant):
    await _ramesh(session, merchant)

    def answer(messages):
        result = last_results(messages)[0]
        assert result["balance"] == "₹500" and result["name"] == "Ramesh Patil"
        return [text("Ramesh Patil ka ₹500 baaki hai.")]

    llm = FakeLLM([tool("get_balance", customer="Ramesh")], answer)
    r = await Orchestrator(llm).handle_text(session, merchant.id, "Ramesh ka kitna baaki hai?", "s1", now=NOW)
    assert r.outcome == "answered" and r.reply == "Ramesh Patil ka ₹500 baaki hai." and len(llm.calls) == 2


async def test_no_db_transaction_held_during_llm_call(session, merchant):
    """A slow LLM call must not keep a write lock: another session can write meanwhile."""
    from app.agent.llm import LLMTurn
    from app.db.base import get_sessionmaker

    class WritesDuringCall:
        async def chat(self, **_):
            async with get_sessionmaker()() as other:
                await ledger.create_customer(other, merchant.id, "Mohan Lal")
                await other.commit()  # "database is locked" if the turn still held its write
            return LLMTurn("ok", [], {"role": "assistant", "content": "ok"})

    r = await Orchestrator(WritesDuringCall()).handle_text(session, merchant.id, "kuch nahi", "s1", now=NOW)
    assert r.reply == "ok"
    assert (await ledger.find_customers(session, merchant.id, "mohan"))["decision"] == "match"


async def test_low_stt_confidence_asks_again(session, merchant):
    llm = FakeLLM()
    r = await Orchestrator(llm).handle_text(session, merchant.id, "rmsh ko pnch", "s1", stt_confidence=0.3, now=NOW)
    assert r.reply == "Phir se boliye?" and not llm.calls
    vlog = (await session.execute(select(VoiceLog))).scalar_one()
    assert vlog.outcome == "low_confidence"


async def test_llm_outage_gives_spoken_fallback(session, merchant):
    class DownLLM:
        async def chat(self, **_):
            raise LLMUnavailable("no credentials")

    r = await Orchestrator(DownLLM()).handle_text(session, merchant.id, "Ramesh ka hisaab", "s1", now=NOW)
    assert r.outcome == "llm_unavailable" and "app se entry" in r.reply


async def test_rate_limit_gives_busy_reply(session, merchant):
    class LimitedLLM:
        async def chat(self, **_):
            raise LLMUnavailable("429", rate_limited=True)

    r = await Orchestrator(LimitedLLM()).handle_text(session, merchant.id, "Ramesh ka hisaab", "s1", now=NOW)
    assert r.outcome == "llm_unavailable" and r.reply == LLM_BUSY_REPLY


async def test_last_payment_date_is_ist(session, merchant):
    """A payment at 00:30 IST is 'today' even though it is still yesterday in UTC."""
    from datetime import datetime as dt, timezone

    from app.agent.tools import ToolContext, execute_tool

    c = await _ramesh(session, merchant)
    e = await ledger.record_payment(session, merchant.id, c.id, 10000)
    e.created_at = dt(2026, 10, 29, 19, 0, tzinfo=timezone.utc)  # 2026-10-30 00:30 IST
    await session.flush()
    ctx = ToolContext(session, merchant, "s1", NOW.date())
    result = await execute_tool(ctx, "get_balance", {"customer": "Ramesh Patil"})
    assert result["last_payment"] == {"amount": "₹100", "on": "2026-10-30"}


async def test_goodbye_ends_without_llm(session, merchant):
    llm = FakeLLM()
    r = await Orchestrator(llm).handle_text(session, merchant.id, "bas, dhanyavaad", "s1", now=NOW)
    assert r.outcome == "ended" and r.reply == "Theek hai, phir milte hain." and not llm.calls


async def test_goodbye_with_haan_saves_pending_then_ends(session, merchant):
    c = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=100)]))
    await orch.handle_text(session, merchant.id, "Ramesh Patil ko 100", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan bas", "s1", now=NOW)
    assert r.outcome == "ended" and r.reply.startswith("Ho gaya.") and r.reply.endswith("phir milte hain.")
    await session.refresh(c)
    assert c.balance_paise == 60000


async def test_plain_goodbye_says_pending_was_not_saved(session, merchant):
    c = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=100)]))
    await orch.handle_text(session, merchant.id, "Ramesh Patil ko 100", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "bas karo", "s1", now=NOW)
    assert r.outcome == "ended" and r.reply == "1 entry save nahi ki. Theek hai, phir milte hain."
    await session.refresh(c)
    assert c.balance_paise == 50000


async def test_goodbye_with_big_amount_asks_once_more(session, merchant):
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=20000)]))
    await _ramesh(session, merchant)
    await orch.handle_text(session, merchant.id, "Ramesh Patil ko bees hazaar", "s1", now=NOW)
    r = await orch.handle_text(session, merchant.id, "haan bas", "s1", now=NOW)
    assert r.outcome == "awaiting_second_confirm"  # conversation stays open until the big amount is confirmed
