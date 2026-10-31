"""§18 Customer notifications + passbook + disputes."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db import base
from app.db.models import Notification, NotificationDelivery
from app.ledger import edits
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.notify import disputes
from app.notify import service as notify
from app.notify.providers import MOCK_SMS, MOCK_WHATSAPP
from app.scheduler.reminders import send_reminders
from tests.conftest import FakeLLM, tool

TZ = get_settings().tz
NOON = datetime(2026, 10, 29, 12, 0, tzinfo=TZ)
PHONE = "+919822001234"


async def _ramesh(session, merchant, phone="9822001234"):
    c = await ledger.create_customer(session, merchant.id, "Ramesh Patil", phone)
    await session.commit()
    return c


async def _notifs(session):
    return list((await session.execute(select(Notification).order_by(Notification.created_at))).scalars())


def _sent(channel=MOCK_WHATSAPP):
    return [m["text"] for m in channel.outbox]


# ---------------------------------------------------------------- every change reaches the customer

async def test_udhaar_by_voice_sends_whatsapp_after_haan(session, merchant):
    await _ramesh(session, merchant)
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Ramesh Patil", amount_rupees=500, due="somvar")]))
    await orch.handle_text(session, merchant.id, "Ramesh ko 500", "s1", now=NOON)
    assert MOCK_WHATSAPP.outbox == []  # nothing before "haan"
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOON)
    [msg] = MOCK_WHATSAPP.outbox
    assert msg["to"] == PHONE and "₹500" in msg["text"] and "Kul baaki ₹500" in msg["text"]
    assert "/passbook" in msg["text"] and "STOP" in msg["text"]  # link + opt-out in the first message
    [n] = await _notifs(session)
    assert n.event_type == "CREDIT"


async def test_notification_is_recorded_before_it_is_sent(session, merchant, monkeypatch):
    c = await _ramesh(session, merchant)
    seen = []

    async def checking_send(to_phone, text):
        async with base.get_sessionmaker()() as other:  # a different connection sees only committed rows
            seen.append(len((await other.execute(select(Notification))).scalars().all()))
        return type("D", (), {"ok": True, "ref": "x", "error": None})()

    monkeypatch.setattr(MOCK_WHATSAPP, "send", checking_send)
    await ledger.add_credit(session, merchant.id, c.id, 30000)
    await notify.commit(session, NOON)
    assert seen == [1]


async def test_partial_payment_message_says_what_is_left(session, merchant):
    c = await _ramesh(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 50000)
    await notify.commit(session, NOON)
    await ledger.record_payment(session, merchant.id, c.id, 30000)
    await notify.commit(session, NOON + timedelta(hours=1))
    n = (await _notifs(session))[-1]
    assert n.event_type == "PARTIAL_PAYMENT" and "₹300 mila" in n.message and "Kul baaki ₹200" in n.message


async def test_edit_is_one_message_with_old_and_new_and_galat_hai(session, merchant):
    c = await _ramesh(session, merchant)
    e = await ledger.add_credit(session, merchant.id, c.id, 50000)
    await notify.commit(session, NOON)
    await edits.edit_entry(session, merchant.id, e.id, amount_paise=60000, reason="galat", edited_by="owner")
    await notify.commit(session, NOON + timedelta(minutes=5))  # within the batch window: edits still go now
    n = (await _notifs(session))[-1]
    assert n.event_type == "EDIT" and "₹500 → ₹600" in n.message and "Galat hai?" in n.message
    assert len(await _notifs(session)) == 2 and len(MOCK_WHATSAPP.outbox) == 2


async def test_upi_auto_settle_notifies_customer(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    mid = await create_merchant(client)
    cid = await create_customer(client, mid, phone="9822001234")
    await give_credit(client, mid, cid, 500)
    await client.post(f"/api/merchants/{mid}/customers/{cid}/upi", json={"vpa": "ramesh@ybl"})
    r = await client.post(f"/api/merchants/{mid}/payments/incoming", json={"payer_vpa": "ramesh@ybl", "amount_rupees": 300})
    assert r.status_code == 200, r.text
    assert any("₹300 mila" in m["text"] for m in MOCK_WHATSAPP.outbox)


# ---------------------------------------------------------------- quiet hours, batching, digest, channels

async def test_quiet_hours_queue_not_drop(session, merchant):
    c = await _ramesh(session, merchant)
    late = datetime(2026, 10, 29, 22, 30, tzinfo=TZ)
    await ledger.add_credit(session, merchant.id, c.id, 30000)
    await notify.commit(session, late)
    assert MOCK_WHATSAPP.outbox == []
    [n] = await _notifs(session)
    assert n.status == "QUEUED" and notify._local(n.send_after) == datetime(2026, 10, 30, 8, 0, tzinfo=TZ)
    assert await notify.dispatch_due(session, datetime(2026, 10, 30, 7, 59, tzinfo=TZ)) == 0
    assert await notify.dispatch_due(session, datetime(2026, 10, 30, 8, 0, tzinfo=TZ)) == 1
    assert len(MOCK_WHATSAPP.outbox) == 1


async def test_batching_within_15_minutes(session, merchant):
    c = await _ramesh(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 30000)
    await notify.commit(session, NOON)                       # first: sent at once
    for minutes, rupees in ((3, 200), (9, 240)):
        await ledger.add_credit(session, merchant.id, c.id, rupees * 100)
        await notify.commit(session, NOON + timedelta(minutes=minutes))
    assert len(MOCK_WHATSAPP.outbox) == 1
    await notify.dispatch_due(session, NOON + timedelta(minutes=15))
    assert len(MOCK_WHATSAPP.outbox) == 2
    combined = MOCK_WHATSAPP.outbox[-1]["text"]
    assert "2 entries" in combined and "₹200 udhaar" in combined and "₹240 udhaar" in combined


async def test_digest_holds_entries_but_edits_go_instantly(session, merchant):
    c = await _ramesh(session, merchant)
    c.notify_pref = "digest"
    e = await ledger.add_credit(session, merchant.id, c.id, 30000)
    await notify.commit(session, NOON)
    assert MOCK_WHATSAPP.outbox == []
    await edits.edit_entry(session, merchant.id, e.id, amount_paise=35000, reason="galat", edited_by="owner")
    await notify.commit(session, NOON + timedelta(hours=1))
    assert len(MOCK_WHATSAPP.outbox) == 1 and "₹350" in MOCK_WHATSAPP.outbox[0]["text"]
    assert await notify.send_digests(session, datetime(2026, 10, 29, 20, 0, tzinfo=TZ)) == 1
    assert "aaj 1 entries" in MOCK_WHATSAPP.outbox[-1]["text"]


async def test_sms_fallback_when_whatsapp_fails(session, merchant):
    c = await _ramesh(session, merchant)
    MOCK_WHATSAPP.fail_for.add(PHONE)
    await ledger.add_credit(session, merchant.id, c.id, 30000)
    await notify.commit(session, NOON)
    assert MOCK_WHATSAPP.outbox == [] and len(MOCK_SMS.outbox) == 1
    rows = (await session.execute(select(NotificationDelivery))).scalars().all()
    assert sorted((r.channel, r.status) for r in rows) == [("sms", "SENT"), ("whatsapp", "FAILED")]


async def test_no_phone_or_opted_out_is_recorded_not_sent(session, merchant):
    nophone = await ledger.create_customer(session, merchant.id, "Mohan Lal")
    stopped = await ledger.create_customer(session, merchant.id, "Sunita Devi", "9890011122")
    stopped.notify_pref = "off"
    await ledger.add_credit(session, merchant.id, nophone.id, 10000)
    await ledger.add_credit(session, merchant.id, stopped.id, 10000)
    await notify.commit(session, NOON)
    assert MOCK_WHATSAPP.outbox == []
    assert sorted(n.status for n in await _notifs(session)) == ["NO_PHONE", "OPTED_OUT"]


async def test_notifications_are_append_only(session, merchant):
    c = await _ramesh(session, merchant)
    await ledger.add_credit(session, merchant.id, c.id, 30000)
    await notify.commit(session, NOON)
    session.info.pop("allow_backdate", None)
    n = (await _notifs(session))[0]
    n.status = "SENT"
    with pytest.raises(LedgerError):
        await session.flush()


# ---------------------------------------------------------------- disputes

async def test_dispute_pauses_reminders_for_that_entry(session, merchant):
    c = await _ramesh(session, merchant)
    disputed = await ledger.add_credit(session, merchant.id, c.id, 30000)
    await ledger.add_credit(session, merchant.id, c.id, 20000)
    await disputes.open_dispute(session, disputed, "customer", "maine nahi liya")
    [r] = await send_reminders(session, merchant, [(c, c.balance_paise, None)])
    assert r.amount_paise == 20000  # only the undisputed entry is asked for
    only = await ledger.create_customer(session, merchant.id, "Suresh", "9822009999")
    e = await ledger.add_credit(session, merchant.id, only.id, 10000)
    await disputes.open_dispute(session, e, "customer", None)
    assert await send_reminders(session, merchant, [(only, only.balance_paise, None)]) == []


async def test_dispute_resolved_by_confirm_or_edit(session, merchant):
    c = await _ramesh(session, merchant)
    a = await ledger.add_credit(session, merchant.id, c.id, 30000)
    b = await ledger.add_credit(session, merchant.id, c.id, 20000)
    d1 = await disputes.open_dispute(session, a, "customer", None)
    d2 = await disputes.open_dispute(session, b, "customer", None)
    await disputes.confirm_dispute(session, merchant.id, d1.id)
    await edits.edit_entry(session, merchant.id, b.id, amount_paise=15000, reason="customer sahi tha", edited_by="owner")
    assert (d1.status, d2.status) == ("CONFIRMED", "EDITED")
    await notify.commit(session, NOON)
    assert any("sahi hai" in m["text"] for m in MOCK_WHATSAPP.outbox)


# ---------------------------------------------------------------- passbook (customer side)

async def _login(client, phone="9822001234"):
    MOCK_SMS.outbox.clear()
    assert (await client.post("/api/passbook/otp", json={"phone": phone})).status_code == 200
    code = next(m["text"].split("code: ")[1][:6] for m in MOCK_SMS.outbox if m["to"].endswith(phone[-10:]))
    r = await client.post("/api/passbook/login", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_passbook_needs_otp_and_shows_only_my_khatas(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    shop1 = await create_merchant(client, name="Sharma Store")
    shop2 = await create_merchant(client, name="Gupta Medical")
    me1 = await create_customer(client, shop1, phone="9822001234")
    me2 = await create_customer(client, shop2, name="Ramesh P", phone="+91 98220 01234")
    other = await create_customer(client, shop1, name="Sunita Devi", phone="9890011122")
    await give_credit(client, shop1, me1, 500)
    await give_credit(client, shop2, me2, 200)
    secret = (await give_credit(client, shop1, other, 900))["entry"]["entry_id"]

    assert (await client.get("/api/passbook")).status_code == 401
    assert (await client.get("/api/passbook", headers={"Authorization": "Bearer nope"})).status_code == 401
    bad = await client.post("/api/passbook/login", json={"phone": "9822001234", "code": "000000"})
    assert bad.status_code == 401
    headers = await _login(client)
    pb = (await client.get("/api/passbook", headers=headers)).json()
    assert sorted(s["shop"] for s in pb["shops"]) == ["Gupta Medical", "Sharma Store"]
    assert pb["total_due"] == "₹700" and "points" in pb
    assert all(e["status"] in ("OPEN", "PARTIAL", "PAID", "REVERSED", None) for s in pb["shops"] for e in s["entries"]
               if e["type"] == "CREDIT")
    assert "₹900" not in str(pb)  # never another customer's data
    r = await client.post("/api/passbook/disputes", json={"entry_id": secret, "reason": "x"}, headers=headers)
    assert r.status_code == 404


async def test_otp_is_single_use(client):
    from tests.api.conftest import create_customer, create_merchant

    mid = await create_merchant(client)
    await create_customer(client, mid, phone="9822001234")
    await client.post("/api/passbook/otp", json={"phone": "9822001234"})
    code = MOCK_SMS.outbox[-1]["text"].split("code: ")[1][:6]
    assert (await client.post("/api/passbook/login", json={"phone": "9822001234", "code": code})).status_code == 200
    assert (await client.post("/api/passbook/login", json={"phone": "9822001234", "code": code})).status_code == 401
    assert (await client.post("/api/passbook/otp", json={"phone": "9000000000"})).json() == {"sent": True}  # no leak


async def test_galat_hai_from_passbook_reaches_the_merchant_and_pauses(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    mid = await create_merchant(client)
    cid = await create_customer(client, mid, phone="9822001234")
    eid = (await give_credit(client, mid, cid, 500))["entry"]["entry_id"]
    headers = await _login(client)
    r = await client.post("/api/passbook/disputes", json={"entry_id": eid, "reason": "maine nahi liya"}, headers=headers)
    assert r.status_code == 201
    [d] = (await client.get(f"/api/merchants/{mid}/disputes")).json()["disputes"]
    assert d["entry_id"] == eid and d["reason"] == "maine nahi liya"
    r = await client.post(f"/api/merchants/{mid}/reminders", json={"customer_ids": [cid]})
    assert r.json()["sent"] == 0  # paused while disputed
    r = await client.post(f"/api/merchants/{mid}/disputes/{d['dispute_id']}/confirm", json={"note": "bill dekha"})
    assert r.json()["status"] == "CONFIRMED"
    assert (await client.post(f"/api/merchants/{mid}/reminders", json={"customer_ids": [cid]})).json()["sent"] == 1


async def test_stop_reply_opts_out(client):
    from tests.api.conftest import create_customer, create_merchant, give_credit

    mid = await create_merchant(client)
    cid = await create_customer(client, mid, phone="9822001234")
    assert (await client.post("/api/notify/inbound", json={"phone": "9822001234", "text": "STOP"})).json() == {"opted_out": 1}
    MOCK_WHATSAPP.outbox.clear()
    await give_credit(client, mid, cid, 100)
    assert MOCK_WHATSAPP.outbox == []


def test_passbook_page_is_light_themed():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "app/web/passbook.html").read_text(encoding="utf-8")
    assert 'content="light"' in html and "prefers-color-scheme" not in html and "Galat hai" in html



