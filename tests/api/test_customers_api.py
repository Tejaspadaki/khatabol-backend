"""CUS: customers, khata entries, UPI links, reversals."""

from datetime import date, timedelta

from app.api.deps import today_ist
from tests.api.conftest import ZERO_ID, create_customer, create_merchant, give_credit


# ---------------------------------------------------------------- create / list

async def test_CUS_01_create_customer(client, mid):
    r = await client.post(f"/api/merchants/{mid}/customers",
                          json={"name": "ramesh  patil", "phone": "9822001234", "nicknames": ["ramu"]})
    assert r.status_code == 201
    c = r.json()
    assert c["name"] == "Ramesh Patil"  # whitespace collapsed, title-cased
    assert c["phone_last4"] == "1234" and c["nicknames"] == ["ramu"]
    assert c["balance"] == "₹0" and c["balance_paise"] == 0


async def test_CUS_02_create_devanagari_name(client, mid):
    c = (await client.post(f"/api/merchants/{mid}/customers", json={"name": "रमेश"})).json()
    assert c["name"] == "रमेश"
    found = (await client.get(f"/api/merchants/{mid}/customers", params={"q": "ramesh"})).json()
    assert found["decision"] == "match"


async def test_CUS_03_create_validation(client, mid):
    assert (await client.post(f"/api/merchants/{mid}/customers", json={})).status_code == 422
    assert (await client.post(f"/api/merchants/{mid}/customers", json={"name": ""})).status_code == 422
    r = await client.post(f"/api/merchants/{mid}/customers", json={"name": "bhaiya ji"})  # only honorifics
    assert r.status_code == 400


async def test_CUS_04_create_for_unknown_merchant(client):
    assert (await client.post(f"/api/merchants/{ZERO_ID}/customers", json={"name": "A"})).status_code == 404


async def test_CUS_05_list_sorted(client, mid):
    for name in ("Sunita Devi", "Mohan Lal", "Ramesh Patil"):
        await create_customer(client, mid, name)
    names = [c["name"] for c in (await client.get(f"/api/merchants/{mid}/customers")).json()["customers"]]
    assert names == ["Mohan Lal", "Ramesh Patil", "Sunita Devi"]


async def test_CUS_06_fuzzy_search(client, mid):
    await create_customer(client, mid, "Ramesh Patil")
    await create_customer(client, mid, "Ramesh Sharma")
    await create_customer(client, mid, "Sunita Devi", nicknames=["sunita didi"])
    q = lambda s: client.get(f"/api/merchants/{mid}/customers", params={"q": s})  # noqa: E731
    ambiguous = (await q("ramesh bhaiya")).json()
    assert ambiguous["decision"] == "ambiguous" and len(ambiguous["customers"]) == 2
    match = (await q("sunitha")).json()
    assert match["decision"] == "match" and match["customers"][0]["name"] == "Sunita Devi"
    assert match["customers"][0]["score"] >= 0.85
    assert (await q("suresh")).json() == {"decision": "no_match", "customers": []}


async def test_CUS_07_list_unknown_merchant(client):
    assert (await client.get(f"/api/merchants/{ZERO_ID}/customers")).status_code == 404


# ---------------------------------------------------------------- detail / history

async def test_CUS_08_customer_detail(client, mid):
    cid = await create_customer(client, mid, phone="9822001234")
    await give_credit(client, mid, cid, 700, due=(today_ist() - timedelta(days=2)).isoformat())
    await give_credit(client, mid, cid, 300, due=(today_ist() + timedelta(days=5)).isoformat())
    await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 200})
    await client.post(f"/api/merchants/{mid}/customers/{cid}/upi", json={"vpa": "Ramesh.P@OKAXIS"})
    d = (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()
    assert d["balance"] == "₹800"
    assert d["overdue"] == "₹500"  # FIFO: the ₹200 payment cleared part of the oldest (overdue) credit
    assert d["next_due_date"] == (today_ist() - timedelta(days=2)).isoformat() and d["next_due"] == "₹500"
    assert d["upi_ids"] == ["ramesh.p@okaxis"] and len(d["ref_code"]) == 5
    assert d["last_payment"]["amount"] == "₹200" and d["last_payment"]["type"] == "PAYMENT"


async def test_CUS_09_detail_unknown_customer(client, mid):
    r = await client.get(f"/api/merchants/{mid}/customers/{ZERO_ID}")
    assert r.status_code == 404 and r.json()["detail"] == "Ye customer is khata mein nahi hai."


async def test_CUS_10_merchants_are_isolated(client, mid):
    cid = await create_customer(client, mid)
    other = await create_merchant(client, name="Other Shop")
    assert (await client.get(f"/api/merchants/{other}/customers/{cid}")).status_code == 404
    r = await client.post(f"/api/merchants/{other}/customers/{cid}/credit", json={"amount_rupees": 10})
    assert r.status_code == 404
    assert (await client.get(f"/api/merchants/{other}/customers")).json()["customers"] == []


async def test_CUS_11_history_newest_first_and_limit(client, mid):
    cid = await create_customer(client, mid)
    await give_credit(client, mid, cid, 100)
    await give_credit(client, mid, cid, 200)
    await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 50})
    entries = (await client.get(f"/api/merchants/{mid}/customers/{cid}/history")).json()["entries"]
    assert [(e["type"], e["amount"]) for e in entries] == [("PAYMENT", "₹50"), ("CREDIT", "₹200"), ("CREDIT", "₹100")]
    assert all(e["source"] == "MANUAL" for e in entries)
    limited = (await client.get(f"/api/merchants/{mid}/customers/{cid}/history", params={"limit": 1})).json()
    assert len(limited["entries"]) == 1


async def test_CUS_12_history_limit_validation(client, mid):
    cid = await create_customer(client, mid)
    for bad in (0, 501):
        r = await client.get(f"/api/merchants/{mid}/customers/{cid}/history", params={"limit": bad})
        assert r.status_code == 422


# ---------------------------------------------------------------- credit / payment

async def test_CUS_13_credit_with_iso_and_spoken_due(client, mid):
    cid = await create_customer(client, mid)
    r = await give_credit(client, mid, cid, 350, due="2030-01-15", items="aata, tel", note="regular")
    assert r["entry"]["due_date"] == "2030-01-15" and r["entry"]["items"] == "aata, tel"
    assert r["customer"]["balance"] == "₹350"
    r = await give_credit(client, mid, cid, 150.5, due="kal")
    assert r["entry"]["due_date"] == (today_ist() + timedelta(days=1)).isoformat()
    assert r["entry"]["amount"] == "₹150.50" and r["customer"]["balance_paise"] == 50050


async def test_CUS_14_credit_unparseable_due(client, mid):
    cid = await create_customer(client, mid)
    r = await client.post(f"/api/merchants/{mid}/customers/{cid}/credit",
                          json={"amount_rupees": 100, "due": "jab paise honge"})
    assert r.status_code == 400 and "samajh nahi aayi" in r.json()["detail"]


async def test_CUS_15_credit_validation(client, mid):
    cid = await create_customer(client, mid)
    for body in ({}, {"amount_rupees": 0}, {"amount_rupees": -5}, {"amount_rupees": "abc"}):
        assert (await client.post(f"/api/merchants/{mid}/customers/{cid}/credit", json=body)).status_code == 422
    r = await client.post(f"/api/merchants/{mid}/customers/{cid}/credit", json={"amount_rupees": 20_000_000})
    assert r.status_code == 400  # over the ₹1 crore sanity cap


async def test_CUS_16_credit_unknown_customer(client, mid):
    r = await client.post(f"/api/merchants/{mid}/customers/{ZERO_ID}/credit", json={"amount_rupees": 10})
    assert r.status_code == 404


async def test_CUS_17_payment_modes_and_advance(client, mid):
    cid = await create_customer(client, mid)
    await give_credit(client, mid, cid, 200)
    r = (await client.post(f"/api/merchants/{mid}/customers/{cid}/payment",
                           json={"amount_rupees": 100, "mode": "upi", "note": "gpay"})).json()
    assert r["entry"]["mode"] == "UPI" and r["customer"]["balance"] == "₹100"
    r = (await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 250})).json()
    assert r["entry"]["mode"] == "CASH"  # default
    assert r["customer"]["balance"] == "-₹150" and r["customer"]["balance_paise"] == -15000  # advance


async def test_CUS_18_payment_validation(client, mid):
    cid = await create_customer(client, mid)
    url = f"/api/merchants/{mid}/customers/{cid}/payment"
    assert (await client.post(url, json={"amount_rupees": 10, "mode": "cheque"})).status_code == 422
    assert (await client.post(url, json={"amount_rupees": 0})).status_code == 422
    assert (await client.post(f"/api/merchants/{mid}/customers/{ZERO_ID}/payment",
                              json={"amount_rupees": 10})).status_code == 404


# ---------------------------------------------------------------- UPI link

async def test_CUS_19_link_upi(client, mid):
    cid = await create_customer(client, mid)
    r = await client.post(f"/api/merchants/{mid}/customers/{cid}/upi", json={"vpa": "  Ramesh@YBL "})
    assert r.status_code == 201 and r.json() == {"customer_id": cid, "vpa": "ramesh@ybl"}


async def test_CUS_20_link_upi_invalid(client, mid):
    cid = await create_customer(client, mid)
    for vpa in ("ramesh", "ramesh @ybl"):
        r = await client.post(f"/api/merchants/{mid}/customers/{cid}/upi", json={"vpa": vpa})
        assert r.status_code == 400
    assert (await client.post(f"/api/merchants/{mid}/customers/{cid}/upi", json={})).status_code == 422


async def test_CUS_21_relink_moves_vpa(client, mid):
    a = await create_customer(client, mid, "Ramesh Patil")
    b = await create_customer(client, mid, "Ramesh Sharma")
    await client.post(f"/api/merchants/{mid}/customers/{a}/upi", json={"vpa": "shared@ybl"})
    await client.post(f"/api/merchants/{mid}/customers/{b}/upi", json={"vpa": "shared@ybl"})
    assert (await client.get(f"/api/merchants/{mid}/customers/{a}")).json()["upi_ids"] == []
    assert (await client.get(f"/api/merchants/{mid}/customers/{b}")).json()["upi_ids"] == ["shared@ybl"]


# ---------------------------------------------------------------- reversal

async def test_CUS_22_reverse_entry(client, mid):
    cid = await create_customer(client, mid)
    await give_credit(client, mid, cid, 500)
    pay = (await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 200})).json()
    eid = pay["entry"]["entry_id"]
    r = await client.post(f"/api/merchants/{mid}/entries/{eid}/reverse")
    assert r.status_code == 201
    assert r.json()["entry"]["type"] == "REVERSAL" and r.json()["customer"]["balance"] == "₹500"
    again = await client.post(f"/api/merchants/{mid}/entries/{eid}/reverse")
    assert again.status_code == 400 and "pehle hi cancel" in again.json()["detail"]
    reversal_id = r.json()["entry"]["entry_id"]
    assert (await client.post(f"/api/merchants/{mid}/entries/{reversal_id}/reverse")).status_code == 400


async def test_CUS_23_reverse_unknown_or_foreign_entry(client, mid):
    assert (await client.post(f"/api/merchants/{mid}/entries/{ZERO_ID}/reverse")).status_code == 404
    cid = await create_customer(client, mid)
    eid = (await give_credit(client, mid, cid, 100))["entry"]["entry_id"]
    other = await create_merchant(client, name="Other Shop")
    assert (await client.post(f"/api/merchants/{other}/entries/{eid}/reverse")).status_code == 404


async def test_CUS_24_balance_survives_mixed_operations(client, mid):
    """End-to-end ledger arithmetic through the API only."""
    cid = await create_customer(client, mid)
    await give_credit(client, mid, cid, 1000, due=(date.today() + timedelta(days=10)).isoformat())
    await give_credit(client, mid, cid, 250.75)
    await client.post(f"/api/merchants/{mid}/customers/{cid}/payment", json={"amount_rupees": 400})
    e = (await give_credit(client, mid, cid, 99))["entry"]["entry_id"]
    await client.post(f"/api/merchants/{mid}/entries/{e}/reverse")
    d = (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()
    assert d["balance_paise"] == 100000 + 25075 - 40000
