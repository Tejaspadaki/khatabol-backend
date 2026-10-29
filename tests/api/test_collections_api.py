"""COL: /dues, /reminders, /summary."""

from urllib.parse import parse_qs, urlparse

from tests.api.conftest import ZERO_ID, create_customer, create_merchant, customer_named, give_credit

# Seeded shop (app/demo/seed.py), relative to today:
#   Ramesh Patil  ₹500 left, overdue (due 2 days ago)
#   Ramesh Sharma ₹1,200, due in 4 days
#   Sunita Devi   ₹450, due today
#   Mohan Lal     ₹250, no due date
#   Pappu         ₹0


async def test_COL_01_dues_filters(client, seeded):
    async def names(f):
        r = await client.get(f"/api/merchants/{seeded}/dues", params={"filter": f})
        assert r.status_code == 200
        return {c["name"]: c["due"] for c in r.json()["customers"]}

    assert await names("overdue") == {"Ramesh Patil": "₹500"}
    assert await names("today") == {"Ramesh Patil": "₹500", "Sunita Devi": "₹450"}
    assert await names("this_week") == {"Ramesh Patil": "₹500", "Sunita Devi": "₹450", "Ramesh Sharma": "₹1,200"}
    assert await names("all") == {"Ramesh Patil": "₹500", "Sunita Devi": "₹450", "Ramesh Sharma": "₹1,200",
                                  "Mohan Lal": "₹250"}


async def test_COL_02_dues_default_sorted_and_total(client, seeded):
    body = (await client.get(f"/api/merchants/{seeded}/dues")).json()
    assert body["filter"] == "all" and body["total_due"] == "₹2,400"
    dues = [c["due_paise"] for c in body["customers"]]
    assert dues == sorted(dues, reverse=True)


async def test_COL_03_dues_validation(client, seeded):
    assert (await client.get(f"/api/merchants/{seeded}/dues", params={"filter": "yesterday"})).status_code == 422


async def test_COL_04_dues_empty_shop(client, mid):
    assert (await client.get(f"/api/merchants/{mid}/dues")).json() == {"filter": "all", "total_due": "₹0", "customers": []}


async def test_COL_05_reminders_all_due(client, seeded):
    r = await client.post(f"/api/merchants/{seeded}/reminders", json={"all_due": True})
    assert r.status_code == 201
    body = r.json()
    assert body["sent"] == 2
    link = body["reminders"][0]["payment_link"]
    q = parse_qs(urlparse(link).query)
    assert link.startswith("upi://pay?") and q["pa"] == ["sharmastore@paytm"] and q["tn"][0].startswith("KHATA-")
    assert all("Namaste" in rem["message"] and rem["status"] == "SENT" for rem in body["reminders"])


async def test_COL_06_reminders_specific_customer(client, seeded):
    mohan = await customer_named(client, seeded, "Mohan Lal")  # seeded without a phone
    body = (await client.post(f"/api/merchants/{seeded}/reminders", json={"customer_ids": [mohan["customer_id"]]})).json()
    assert body["sent"] == 1 and body["reminders"][0]["amount"] == "₹250"
    assert body["reminders"][0]["status"] == "NO_PHONE"
    assert "am=250.00" in body["reminders"][0]["payment_link"]


async def test_COL_07_reminders_skip_zero_balance(client, seeded):
    pappu = await customer_named(client, seeded, "Pappu")
    body = (await client.post(f"/api/merchants/{seeded}/reminders", json={"customer_ids": [pappu["customer_id"]]})).json()
    assert body["sent"] == 0


async def test_COL_08_reminders_validation(client, seeded):
    url = f"/api/merchants/{seeded}/reminders"
    assert (await client.post(url, json={})).status_code == 422
    assert (await client.post(url, json={"customer_ids": ["nope"]})).status_code == 422
    assert (await client.post(url, json={"customer_ids": [ZERO_ID]})).status_code == 404


async def test_COL_09_reminders_need_merchant_vpa(client):
    mid = await create_merchant(client, upi_vpa=None)
    cid = await create_customer(client, mid)
    await give_credit(client, mid, cid, 100)
    r = await client.post(f"/api/merchants/{mid}/reminders", json={"customer_ids": [cid]})
    assert r.status_code == 400 and "UPI ID" in r.json()["detail"]


async def test_COL_10_summary_today(client, seeded):
    await client.post(f"/api/merchants/{seeded}/payments/incoming",
                      json={"payer_vpa": "ramesh.p@okaxis", "amount_rupees": 300})
    s = (await client.get(f"/api/merchants/{seeded}/summary")).json()
    assert s["collected"] == "₹300" and s["collected_upi_paise"] == 30000
    assert s["outstanding"] == "₹2,100"
    assert s["spoken"].startswith("Aaj ") and "₹2,100 baaki hai" in s["spoken"]


async def test_COL_11_summary_past_day_and_validation(client, seeded):
    s = (await client.get(f"/api/merchants/{seeded}/summary", params={"day": "2020-01-01"})).json()
    assert s["date"] == "2020-01-01" and s["credit_given"] == "₹0" and s["entries"] == 0
    assert (await client.get(f"/api/merchants/{seeded}/summary", params={"day": "31-12-2020"})).status_code == 422
