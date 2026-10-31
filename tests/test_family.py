"""§17 Family (Parivaar) Khata."""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.models import FamilyMerge, LedgerEntry
from app.family import service as family
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.notify import service as notify
from app.notify.providers import MOCK_WHATSAPP
from app.payments import reconciler
from tests.conftest import FakeLLM, last_results, text, tool

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=get_settings().tz)


async def _patil(session, merchant):
    """Patil parivar: head Ramesh (phone), son Pinku (no phone), daughter-in-law Kavita (phone)."""
    head = await ledger.create_customer(session, merchant.id, "Ramesh Patil", "9822001234")
    f = await family.create_family(session, merchant.id, "Patil parivar", head.id, area_hint="gali no. 3")
    pinku = await family.add_member(session, merchant.id, f.id, "Pinku", "beta")
    kavita = await family.add_member(session, merchant.id, f.id, "Kavita", "bahu", phone="9890011122")
    await session.commit()
    return f, head, pinku, kavita


# ---------------------------------------------------------------- resolution

async def test_ramesh_ji_ke_bete_ne_200_liya(session, merchant):
    f, head, pinku, _ = await _patil(session, merchant)

    def credit(messages):
        res = last_results(messages)[0]
        assert res["member_id"] == str(pinku.id) and res["family_id"] == str(f.id)
        return [tool("add_credit", customer=res["member_id"], amount_rupees=200)]

    orch = Orchestrator(FakeLLM([tool("resolve_party", anchor_name="Ramesh ji", relation="bete")], credit))
    r = await orch.handle_text(session, merchant.id, "Ramesh ji ke bete ne 200 ka liya", "s1", now=NOW)
    assert "Patil parivar" in r.pending[0]["summary"] and "Pinku" in r.pending[0]["summary"]
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    e = (await session.execute(select(LedgerEntry))).scalar_one()
    assert (e.customer_id, e.family_id, e.taken_by_customer_id) == (head.id, f.id, pinku.id)
    await session.refresh(pinku)
    found = await ledger.find_customers(session, merchant.id, "Ramesh ji ke bete")  # the spoken form is learned
    assert found["decision"] == "match" and found["matches"][0]["customer"].id == pinku.id


async def test_person_name_finds_their_family(session, merchant):
    f, head, pinku, _ = await _patil(session, merchant)
    res = await family.resolve_party(session, merchant.id, person_name="Pinku")
    assert (res["family_id"], res["member_id"]) == (str(f.id), str(pinku.id))


async def test_two_families_with_one_name_ask_with_area(session, merchant):
    await _patil(session, merchant)
    other = await ledger.create_customer(session, merchant.id, "Suresh Patil")
    await family.create_family(session, merchant.id, "Patil parivar", other.id, area_hint="station road")
    await session.commit()
    with pytest.raises(LedgerError) as exc:
        await family.resolve_party(session, merchant.id, family_hint="Patil parivar")
    assert "gali no. 3" in str(exc.value) and "station road" in str(exc.value)


async def test_unknown_relation_offers_to_add(session, merchant):
    await _patil(session, merchant)
    with pytest.raises(LedgerError) as exc:
        await family.resolve_party(session, merchant.id, anchor_name="Ramesh", relation="beti")
    assert "Add karun" in str(exc.value) and "beti" in str(exc.value)


# ---------------------------------------------------------------- guardrails (in the write path)

async def test_member_without_permission_needs_explicit_override(session, merchant):
    f, head, pinku, _ = await _patil(session, merchant)
    pinku.can_take_credit = False
    await session.commit()
    with pytest.raises(LedgerError) as exc:
        await ledger.add_credit(session, merchant.id, pinku.id, 10000)
    assert exc.value.key == "err_member_no_permission"
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Pinku", amount_rupees=100)]))
    r = await orch.handle_text(session, merchant.id, "Pinku ne 100 liya", "s1", now=NOW)
    assert r.pending[0]["needs_double_confirmation"]
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "awaiting_second_confirm" and "permission" in r.reply.lower()
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "committed"


async def test_member_and_family_limits_block_without_override(session, merchant):
    f, head, pinku, kavita = await _patil(session, merchant)
    pinku.member_limit_paise = 20000
    f.credit_limit_paise = 50000
    await session.commit()
    await ledger.add_credit(session, merchant.id, pinku.id, 15000)
    with pytest.raises(LedgerError) as exc:
        await ledger.add_credit(session, merchant.id, pinku.id, 10000)
    assert exc.value.key == "err_member_over_limit"
    await ledger.add_credit(session, merchant.id, pinku.id, 10000, allow_member_over_paise=5000)
    with pytest.raises(LedgerError) as exc:
        await ledger.add_credit(session, merchant.id, kavita.id, 30000)
    assert exc.value.key == "err_family_over_limit"


async def test_family_limit_override_by_voice_names_the_overage(session, merchant):
    f, head, pinku, kavita = await _patil(session, merchant)
    f.credit_limit_paise = 50000
    await ledger.add_credit(session, merchant.id, kavita.id, 40000)
    await session.commit()
    orch = Orchestrator(FakeLLM([tool("add_credit", customer="Kavita", amount_rupees=300)]))
    r = await orch.handle_text(session, merchant.id, "Kavita ne 300 liya", "s1", now=NOW)
    assert "₹200" in r.pending[0]["summary"]
    r = await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    assert r.outcome == "awaiting_second_confirm" and "₹200" in r.reply
    assert (await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)).outcome == "committed"


# ---------------------------------------------------------------- balance, payments, notifications

async def test_family_balance_is_the_sum_of_members(session, merchant):
    f, head, pinku, kavita = await _patil(session, merchant)
    await ledger.add_credit(session, merchant.id, pinku.id, 20000)
    await ledger.add_credit(session, merchant.id, kavita.id, 30000)
    await ledger.add_credit(session, merchant.id, head.id, 10000)
    fam_only = await family.family_credit(session, merchant.id, f.id, 5000)  # "Patil parivar mein 50 likho"
    assert fam_only.taken_by_customer_id is None
    b = await family.family_balance(session, merchant.id, f.id)
    assert b["total_paise"] == 65000 == sum(m["owed_paise"] for m in b["members"]) + b["unassigned_paise"]
    assert {m["name"]: m["owed_paise"] for m in b["members"]} == {"Ramesh Patil": 10000, "Pinku": 20000, "Kavita": 30000}


async def test_member_upi_payment_settles_family_oldest_first(session, merchant):
    f, head, pinku, kavita = await _patil(session, merchant)
    first = await ledger.add_credit(session, merchant.id, pinku.id, 20000)
    await ledger.add_credit(session, merchant.id, kavita.id, 30000)
    await ledger.link_upi(session, merchant.id, kavita.id, "kavita@ybl")
    await session.commit()
    res = await reconciler.handle_payment(session, reconciler.PaymentEvent(merchant.id, "T1", "kavita@ybl", 25000))
    assert res.txn.status.value == "APPLIED"
    st = await ledger.entry_statuses(session, head.id)
    assert st[first.id] == ("PAID", 0)
    assert head.balance_paise == 25000


async def test_head_and_member_with_phone_are_notified(session, merchant):
    f, head, pinku, kavita = await _patil(session, merchant)
    await ledger.add_credit(session, merchant.id, kavita.id, 30000)
    await ledger.add_credit(session, merchant.id, pinku.id, 10000)  # Pinku has no phone: only the head
    await notify.commit(session, NOW)
    to = sorted(m["to"] for m in MOCK_WHATSAPP.outbox)
    assert to == ["+919822001234", "+919890011122"]


async def test_member_leaves_future_udhaar_goes_to_own_khata(session, merchant):
    f, head, pinku, _ = await _patil(session, merchant)
    old = await ledger.add_credit(session, merchant.id, pinku.id, 20000)
    await family.remove_member(session, merchant.id, pinku.id)
    new = await ledger.add_credit(session, merchant.id, pinku.id, 5000)
    assert old.customer_id == head.id and new.customer_id == pinku.id
    assert head.balance_paise == 20000 and pinku.balance_paise == 5000


async def test_merge_moves_members_entries_and_logs(session, merchant):
    f, head, pinku, _ = await _patil(session, merchant)
    dup_head = await ledger.create_customer(session, merchant.id, "Ramesh P")
    dup = await family.create_family(session, merchant.id, "Patil family", dup_head.id)
    await ledger.add_credit(session, merchant.id, dup_head.id, 7000)
    await session.commit()
    await family.merge_families(session, merchant.id, dup.id, f.id, merged_by="owner")
    await session.refresh(dup_head)
    assert dup_head.family_id == f.id and dup_head.balance_paise == 0
    assert (await family.family_balance(session, merchant.id, f.id))["total_paise"] == 7000
    [log] = (await session.execute(select(FamilyMerge))).scalars().all()
    assert log.from_family_id == dup.id and len(log.moved["entries"]) == 1


async def test_dynamic_family_qr_has_due_amount_and_ref(session, merchant):
    f, head, pinku, _ = await _patil(session, merchant)
    await ledger.add_credit(session, merchant.id, pinku.id, 45000)
    qr = await family.family_pay_link(session, merchant, f.id)
    assert "am=450.00" in qr["upi_link"] and f"KHATA-{head.ref_code}" in qr["upi_link"]
    assert qr["amount"] == "₹450"


async def test_get_family_balance_tool(session, merchant):
    f, head, pinku, _ = await _patil(session, merchant)
    await ledger.add_credit(session, merchant.id, pinku.id, 20000)
    await session.commit()

    def speak(messages):
        res = last_results(messages)[0]
        assert res["total"] == "₹200" and res["members"][1]["name"] == "Pinku"
        return [text("Patil parivar ka ₹200 baaki.")]

    await Orchestrator(FakeLLM([tool("get_family_balance", family="Patil parivar")], speak)).handle_text(
        session, merchant.id, "Patil parivar ka hisaab", "s1", now=NOW)


async def test_create_family_and_add_member_by_voice(session, merchant):
    await ledger.create_customer(session, merchant.id, "Mohan Sharma")
    await session.commit()
    orch = Orchestrator(FakeLLM([tool("create_family", name="Sharma parivar", head="Mohan Sharma", area_hint="gali 5")]))
    await orch.handle_text(session, merchant.id, "Sharma parivar banao, Mohan head", "s1", now=NOW)
    await orch.handle_text(session, merchant.id, "haan", "s1", now=NOW)
    orch = Orchestrator(FakeLLM([tool("add_member", family="Sharma parivar", name="Golu", relation="beta")]))
    await orch.handle_text(session, merchant.id, "Sharma parivar mein Golu beta add karo", "s2", now=NOW)
    await orch.handle_text(session, merchant.id, "haan", "s2", now=NOW)
    res = await family.resolve_party(session, merchant.id, anchor_name="Mohan", relation="beta")
    assert res["member"] == "Golu"


async def test_api_family_view_and_qr(client):
    from tests.api.conftest import create_customer, create_merchant

    mid = await create_merchant(client)
    head = await create_customer(client, mid, name="Ramesh Patil", phone="9822001234")
    r = await client.post(f"/api/merchants/{mid}/agent/propose", json={
        "tool": "create_family", "session_id": "f1", "args": {"name": "Patil parivar", "head": head}})
    assert r.status_code == 200, r.text
    await client.post(f"/api/merchants/{mid}/agent/message", json={"text": "haan", "session_id": "f1"})
    [fam] = (await client.get(f"/api/merchants/{mid}/families")).json()["families"]
    detail = (await client.get(f"/api/merchants/{mid}/families/{fam['family_id']}")).json()
    assert detail["name"] == "Patil parivar" and detail["members"][0]["relation"] == "self"
    assert (await client.get(f"/api/merchants/{mid}/families/{fam['family_id']}/qr")).status_code == 200
