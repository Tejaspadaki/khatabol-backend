"""§19 Edit and update records: reversal + corrected entry, entry_edits, approval, old-entry confirmation."""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.agent import pending as gate
from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.models import EntryEdit, EntryType, LedgerEntry, PendingAction, PendingStatus
from app.ledger import edits
from app.ledger import service as ledger
from tests.conftest import FakeLLM, tool

TZ = get_settings().tz
NOW = datetime(2026, 10, 29, 11, 0, tzinfo=TZ)


async def _ramesh(session, merchant, rupees=500, days_ago=0, due=None):
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil", "9822001234")
    e = await ledger.add_credit(session, merchant.id, c.id, rupees * 100, due_date=due, items="aata")
    e.created_at = NOW - timedelta(days=days_ago)
    await session.commit()
    return c, e


def _snapshot(e: LedgerEntry):
    return (e.type, e.amount_paise, e.due_date, e.items, e.customer_id, e.created_at)


async def _entries(session, customer_id):
    stmt = select(LedgerEntry).where(LedgerEntry.customer_id == customer_id).order_by(LedgerEntry.created_at)
    return list((await session.execute(stmt)).scalars())


async def _edits(session):
    return list((await session.execute(select(EntryEdit).order_by(EntryEdit.created_at))).scalars())


# ---------------------------------------------------------------- service

async def test_edit_amount_is_reversal_plus_new_entry_and_original_untouched(session, merchant):
    c, e = await _ramesh(session, merchant)
    await session.refresh(e)  # compare the stored row with itself (SQLite returns naive timestamps)
    before = _snapshot(e)
    reversal, new, rows = await edits.edit_entry(session, merchant.id, e.id, amount_paise=60000,
                                                 reason="wrong amount", edited_by="owner")
    await session.commit()
    await session.refresh(e)
    assert _snapshot(e) == before
    assert reversal.type == EntryType.REVERSAL and reversal.reverses_entry_id == e.id
    assert new.type == EntryType.CREDIT and new.amount_paise == 60000 and new.items == "aata"
    assert c.balance_paise == 60000
    [row] = await _edits(session)
    assert (row.field_changed, row.old_value, row.new_value, row.reason, row.edited_by) == (
        "amount", "₹500", "₹600", "wrong amount", "owner")
    assert (row.original_entry_id, row.reversal_entry_id, row.new_entry_id) == (e.id, reversal.id, new.id)


async def test_move_entry_to_another_customer(session, merchant):
    c, e = await _ramesh(session, merchant)
    suresh = await ledger.create_customer(session, merchant.id, "Suresh Kumar")
    _, new, _ = await edits.edit_entry(session, merchant.id, e.id, to_customer_id=suresh.id,
                                       reason="wrong customer", edited_by="owner")
    assert new.customer_id == suresh.id and c.balance_paise == 0 and suresh.balance_paise == 50000


async def test_partial_payment_follows_the_corrected_entry(session, merchant):
    c, e = await _ramesh(session, merchant)
    await ledger.record_payment(session, merchant.id, c.id, 20000)
    _, new, _ = await edits.edit_entry(session, merchant.id, e.id, amount_paise=60000, reason="wrong amount",
                                       edited_by="owner")
    st = await ledger.entry_statuses(session, c.id)
    assert st[new.id] == ("PARTIAL", 40000) and st[e.id][0] == "REVERSED"


async def test_edit_never_bypasses_the_limit_but_ignores_the_hold(session, merchant):
    c, e = await _ramesh(session, merchant)
    await ledger.set_hold(session, merchant.id, c.id, True, "jhagda", "merchant", NOW.date())
    _, new, _ = await edits.edit_entry(session, merchant.id, e.id, amount_paise=40000, reason="wrong amount",
                                       edited_by="owner")  # a correction is not new udhaar
    assert new.amount_paise == 40000
    await ledger.set_credit_limit(session, merchant.id, c.id, 45000)
    try:
        await edits.edit_entry(session, merchant.id, new.id, amount_paise=90000, reason="wrong amount", edited_by="owner")
        raise AssertionError("over-limit edit must fail")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "key", None) == "err_over_limit"


async def test_note_history_without_touching_the_ledger(session, merchant):
    c, e = await _ramesh(session, merchant)
    await edits.add_note(session, merchant.id, e.id, "bete ne liya", edited_by="owner")
    await edits.add_note(session, merchant.id, e.id, "bahu ne liya", edited_by="owner")
    assert len(await _entries(session, c.id)) == 1
    hist = await edits.entry_history(session, merchant.id, e.id)
    assert [(h.old_value, h.new_value) for h in hist] == [(None, "bete ne liya"), ("bete ne liya", "bahu ne liya")]


# ---------------------------------------------------------------- voice flow + trust rules

async def test_voice_pichhli_entry_500_nahi_600_thi(session, merchant):
    c, e = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("edit_entry", amount_rupees=600, reason="galat amount")]))
    r = await orch.handle_text(session, merchant.id, "Pichhli entry 500 nahi 600 thi", "s1", now=NOW)
    assert r.outcome == "awaiting_confirmation"
    assert "₹500" in r.reply and "₹600" in r.reply
    assert len(await _entries(session, c.id)) == 1  # nothing written before "haan"
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"
    await session.refresh(c)
    assert c.balance_paise == 60000 and len(await _edits(session)) == 1


async def test_give_more_time_extends_due_date_with_history(session, merchant):
    due = NOW.date() + timedelta(days=3)
    c, e = await _ramesh(session, merchant, due=due)
    orch = Orchestrator(FakeLLM([tool("edit_entry", customer="Ramesh Patil", extend_days=10, reason="aur samay")]))
    await orch.handle_text(session, merchant.id, "Ramesh ko 10 din aur de do", "s1", now=NOW)
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    b = await ledger.get_balance(session, merchant.id, c.id, NOW.date())
    assert b["next_due_date"] == due + timedelta(days=10)
    [row] = await _edits(session)
    assert row.field_changed == "due_date" and row.old_value == due.isoformat()


async def test_waiver_by_voice_needs_reason(session, merchant):
    c, e = await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("waive_amount", customer="Ramesh Patil", amount_rupees=50, reason="chhod diye")]))
    r = await orch.handle_text(session, merchant.id, "Ramesh ke 50 chhod do", "s1", now=NOW)
    assert "₹50" in r.pending[0]["summary"]
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    await session.refresh(c)
    assert c.balance_paise == 45000
    kinds = [x.type for x in await _entries(session, c.id)]
    assert EntryType.ADJUSTMENT in kinds


async def test_entry_older_than_7_days_needs_second_confirmation(session, merchant):
    c, e = await _ramesh(session, merchant, days_ago=10)
    orch = Orchestrator(FakeLLM([tool("edit_entry", customer="Ramesh Patil", amount_rupees=600, reason="galat")]))
    r = await orch.handle_text(session, merchant.id, "Ramesh wali entry 600 thi", "s1", now=NOW)
    assert r.pending[0]["needs_double_confirmation"]
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "awaiting_second_confirm"
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"


async def test_staff_edit_waits_for_owner_approval(session, merchant):
    c, e = await _ramesh(session, merchant)
    staff = await edits.add_staff(session, merchant.id, "Raju", "staff")
    await session.commit()
    orch = Orchestrator(FakeLLM([tool("edit_entry", amount_rupees=600, reason="galat")]))
    await orch.handle_text(session, merchant.id, "pichhli entry 600 thi", "s1", now=NOW, actor=f"staff:{staff.id}")
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW, actor=f"staff:{staff.id}")
    assert r.outcome == "awaiting_approval"
    assert len(await _entries(session, c.id)) == 1
    pa = (await session.execute(select(PendingAction))).scalar_one()
    assert pa.status == PendingStatus.AWAITING_APPROVAL

    try:  # staff cannot approve their own edit
        await gate.approve(session, merchant, pa.id, approver=f"staff:{staff.id}")
        raise AssertionError("staff approval must fail")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "key", None) == "err_owner_only"
    out = await gate.approve(session, merchant, pa.id, approver="owner")
    await session.commit()
    await session.refresh(c)
    assert c.balance_paise == 60000 and out["status"] == "COMMITTED"
    [row] = await _edits(session)
    assert row.edited_by == f"staff:{staff.id}" and row.approved_by == "owner"


# ---------------------------------------------------------------- REST: app edit through the gate + history

async def test_api_app_edit_goes_through_gate_and_history_lists_it(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    mid = await create_merchant(client)
    cid = await create_customer(client, mid)
    eid = (await give_credit(client, mid, cid, 500))["entry"]["entry_id"]
    r = await client.post(f"/api/merchants/{mid}/agent/propose", json={
        "tool": "edit_entry", "session_id": "edit1",
        "args": {"entry": eid, "amount_rupees": 600, "reason": "wrong amount"}})
    assert r.status_code == 200 and r.json()["outcome"] == "awaiting_confirmation", r.text
    assert (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()["balance_paise"] == 50000
    r = await client.post(f"/api/merchants/{mid}/agent/message", json={"text": "haan", "session_id": "edit1"})
    assert r.json()["outcome"] == "committed"
    hist = (await client.get(f"/api/merchants/{mid}/entries/{eid}/edits")).json()["edits"]
    assert hist[0]["field"] == "amount" and hist[0]["old"] == "₹500" and hist[0]["new"] == "₹600"
    assert hist[0]["by"] == "owner" and hist[0]["at"]


async def test_api_staff_and_approvals(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    mid = await create_merchant(client)
    cid = await create_customer(client, mid)
    eid = (await give_credit(client, mid, cid, 500))["entry"]["entry_id"]
    sid = (await client.post(f"/api/merchants/{mid}/staff", json={"name": "Raju", "role": "staff"})).json()["staff_id"]
    await client.post(f"/api/merchants/{mid}/agent/propose", json={
        "tool": "edit_entry", "session_id": "st1", "staff_id": sid,
        "args": {"entry": eid, "amount_rupees": 600, "reason": "wrong amount"}})
    r = await client.post(f"/api/merchants/{mid}/agent/message", json={"text": "haan", "session_id": "st1", "staff_id": sid})
    assert r.json()["outcome"] == "awaiting_approval"
    [ap] = (await client.get(f"/api/merchants/{mid}/approvals")).json()["approvals"]
    assert (await client.post(f"/api/merchants/{mid}/approvals/{ap['action_id']}/approve",
                              headers={"X-Staff-Id": sid})).status_code == 403
    r = await client.post(f"/api/merchants/{mid}/approvals/{ap['action_id']}/approve")
    assert r.status_code == 200
    assert (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()["balance_paise"] == 60000
