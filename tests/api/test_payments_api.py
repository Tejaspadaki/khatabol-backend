"""PAY: incoming UPI payments (simulated), unmatched queue, assign, undo."""

from tests.api.conftest import ZERO_ID, create_customer, customer_named, give_credit


def incoming(client, mid, **body):
    return client.post(f"/api/merchants/{mid}/payments/incoming", json=body)


async def balance(client, mid, cid) -> str:
    return (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()["balance"]


async def test_PAY_01_linked_payer_auto_settles(client, seeded):
    r = await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=300, txn_id="T1")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "APPLIED" and body["duplicate"] is False
    assert body["announcement"] == "Ramesh Patil ne ₹300 UPI se diye. Ab ₹200 baaki."
    ramesh = await customer_named(client, seeded, "Ramesh Patil")
    assert body["customer_id"] == ramesh["customer_id"] and ramesh["balance"] == "₹200"
    entry = (await client.get(f"/api/merchants/{seeded}/customers/{ramesh['customer_id']}/history")).json()["entries"][0]
    assert entry["source"] == "UPI_AUTO" and entry["mode"] == "UPI"


async def test_PAY_02_duplicate_txn_is_ignored(client, seeded):
    await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=300, txn_id="T1")
    body = (await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=300, txn_id="T1")).json()
    assert body["duplicate"] is True and body["announcement"] is None
    assert (await customer_named(client, seeded, "Ramesh Patil"))["balance"] == "₹200"


async def test_PAY_03_generated_txn_ids_are_unique(client, seeded):
    a = (await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=10)).json()
    b = (await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=10)).json()
    assert a["txn_id"].startswith("SIM") and a["txn_id"] != b["txn_id"]


async def test_PAY_04_unknown_payer_with_dues_is_unmatched(client, seeded):
    body = (await incoming(client, seeded, payer_vpa="Sunita99@YBL", amount_rupees=200, txn_id="T2")).json()
    assert body["status"] == "UNMATCHED" and body["payer_vpa"] == "sunita99@ybl"
    assert body["announcement"] == "₹200 sunita99@ybl se aaye. Kiska hai?" and body["customer_id"] is None


async def test_PAY_05_unknown_payer_without_dues_is_sale(client, mid):
    body = (await incoming(client, mid, payer_vpa="walkin@ybl", amount_rupees=50)).json()
    assert body["status"] == "SALE" and body["announcement"] == "₹50 prapt hue."


async def test_PAY_06_linked_payer_without_dues_is_sale(client, mid):
    cid = await create_customer(client, mid)
    await client.post(f"/api/merchants/{mid}/customers/{cid}/upi", json={"vpa": "ramesh@ybl"})
    body = (await incoming(client, mid, payer_vpa="ramesh@ybl", amount_rupees=80)).json()
    assert body["status"] == "SALE" and await balance(client, mid, cid) == "₹0"


async def test_PAY_07_reminder_ref_in_note_matches_and_learns_vpa(client, seeded):
    mohan = await customer_named(client, seeded, "Mohan Lal")
    link = (await client.post(f"/api/merchants/{seeded}/reminders",
                              json={"customer_ids": [mohan["customer_id"]]})).json()["reminders"][0]["payment_link"]
    note = link.split("tn=")[1]
    body = (await incoming(client, seeded, payer_vpa="mohan@ybl", amount_rupees=250, note=note)).json()
    assert body["status"] == "APPLIED" and body["customer_id"] == mohan["customer_id"]
    detail = (await client.get(f"/api/merchants/{seeded}/customers/{mohan['customer_id']}")).json()
    assert detail["upi_ids"] == ["mohan@ybl"] and detail["balance"] == "₹0"


async def test_PAY_08_overpayment_becomes_advance(client, seeded):
    body = (await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=600)).json()
    assert body["announcement"] == "Ramesh Patil ne ₹600 UPI se diye. ₹100 advance jama hai."


async def test_PAY_09_incoming_validation(client, seeded):
    for bad in ({"payer_vpa": "a@b", "amount_rupees": 0}, {"payer_vpa": "ab", "amount_rupees": 10},
                {"amount_rupees": 10}, {"payer_vpa": "a@b"}):
        assert (await incoming(client, seeded, **bad)).status_code == 422
    assert (await incoming(client, ZERO_ID, payer_vpa="a@b", amount_rupees=1)).status_code == 404


async def test_PAY_10_unmatched_queue(client, seeded):
    assert (await client.get(f"/api/merchants/{seeded}/payments/unmatched")).json() == {"payments": []}
    await incoming(client, seeded, payer_vpa="x1@ybl", amount_rupees=10, txn_id="U1")
    await incoming(client, seeded, payer_vpa="x2@ybl", amount_rupees=20, txn_id="U2")
    await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=30, txn_id="A1")  # applied, not listed
    txns = {p["txn_id"] for p in (await client.get(f"/api/merchants/{seeded}/payments/unmatched")).json()["payments"]}
    assert txns == {"U1", "U2"}


async def test_PAY_11_assign_then_auto_match(client, seeded):
    sunita = await customer_named(client, seeded, "Sunita Devi")
    await incoming(client, seeded, payer_vpa="sunita99@ybl", amount_rupees=200, txn_id="T2")
    r = await client.post(f"/api/merchants/{seeded}/payments/T2/assign", json={"customer_id": sunita["customer_id"]})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "RESOLVED" and body["customer"]["balance"] == "₹250"
    assert body["spoken"] == "₹200 Sunita Devi ke khate mein, ab ₹250 baaki."
    assert (await client.get(f"/api/merchants/{seeded}/payments/unmatched")).json()["payments"] == []
    nxt = (await incoming(client, seeded, payer_vpa="sunita99@ybl", amount_rupees=50)).json()
    assert nxt["status"] == "APPLIED" and nxt["customer_id"] == sunita["customer_id"]


async def test_PAY_12_assign_errors(client, seeded):
    sunita = await customer_named(client, seeded, "Sunita Devi")
    url = f"/api/merchants/{seeded}/payments/{{}}/assign"
    assert (await client.post(url.format("NOPE"), json={"customer_id": sunita["customer_id"]})).status_code == 404
    await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=10, txn_id="A1")
    r = await client.post(url.format("A1"), json={"customer_id": sunita["customer_id"]})
    assert r.status_code == 400 and "pehle hi" in r.json()["detail"]
    await incoming(client, seeded, payer_vpa="x@ybl", amount_rupees=10, txn_id="U1")
    assert (await client.post(url.format("U1"), json={"customer_id": ZERO_ID})).status_code == 404
    assert (await client.post(url.format("U1"), json={})).status_code == 422


async def test_PAY_13_undo_wrong_match_and_reassign(client, seeded):
    ramesh = await customer_named(client, seeded, "Ramesh Patil")
    sunita = await customer_named(client, seeded, "Sunita Devi")
    await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=300, txn_id="T5")
    r = await client.post(f"/api/merchants/{seeded}/payments/T5/undo")
    assert r.status_code == 200 and r.json()["status"] == "UNMATCHED" and r.json()["customer_id"] is None
    detail = (await client.get(f"/api/merchants/{seeded}/customers/{ramesh['customer_id']}")).json()
    assert detail["balance"] == "₹500" and detail["upi_ids"] == []
    body = (await client.post(f"/api/merchants/{seeded}/payments/T5/assign",
                              json={"customer_id": sunita["customer_id"]})).json()
    assert body["customer"]["balance"] == "₹150"


async def test_PAY_14_undo_errors(client, seeded):
    assert (await client.post(f"/api/merchants/{seeded}/payments/NOPE/undo")).status_code == 404
    await incoming(client, seeded, payer_vpa="x@ybl", amount_rupees=10, txn_id="U1")
    r = await client.post(f"/api/merchants/{seeded}/payments/U1/undo")
    assert r.status_code == 400 and "koi entry nahi" in r.json()["detail"]


async def test_PAY_15_payment_is_tied_to_one_merchant(client, seeded, mid):
    await incoming(client, seeded, payer_vpa="x@ybl", amount_rupees=10, txn_id="U1")
    sunita = await customer_named(client, seeded, "Sunita Devi")
    assert (await client.post(f"/api/merchants/{mid}/payments/U1/assign",
                              json={"customer_id": sunita["customer_id"]})).status_code == 404
    cid = await create_customer(client, mid)
    await give_credit(client, mid, cid, 100)
    assert (await client.post(f"/api/merchants/{seeded}/payments/U1/assign",
                              json={"customer_id": cid})).status_code == 404


async def test_PAY_EN_announcement_in_merchant_language(client, seeded):
    await client.patch(f"/api/merchants/{seeded}", json={"language": "en"})
    body = (await incoming(client, seeded, payer_vpa="ramesh.p@okaxis", amount_rupees=300, txn_id="TEN")).json()
    assert body["announcement"] == "Ramesh Patil paid ₹300 by UPI. Now ₹200 due."
