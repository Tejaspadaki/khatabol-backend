"""MER: /api/merchants"""

from tests.api.conftest import ZERO_ID, create_merchant


async def test_MER_01_create_merchant(client):
    r = await client.post("/api/merchants", json={
        "name": "Sharma General Store", "phone": "9876500000", "upi_vpa": "sharma@paytm",
        "language": "hi", "large_amount_threshold_rupees": 5000,
    })
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Sharma General Store" and body["upi_vpa"] == "sharma@paytm"
    assert body["language"] == "hi" and body["large_amount_threshold"] == "₹5,000"
    assert len(body["merchant_id"]) == 36


async def test_MER_02_create_defaults(client):
    body = (await client.post("/api/merchants", json={"name": "Chai Stall"})).json()
    assert body["language"] == "hi" and body["large_amount_threshold"] == "₹10,000" and body["upi_vpa"] is None


async def test_MER_03_create_validation(client):
    assert (await client.post("/api/merchants", json={})).status_code == 422
    assert (await client.post("/api/merchants", json={"name": ""})).status_code == 422
    assert (await client.post("/api/merchants", json={"name": "X", "language": "fr"})).status_code == 422
    assert (await client.post("/api/merchants", json={"name": "X", "large_amount_threshold_rupees": 0})).status_code == 422


async def test_MER_04_get_merchant(client):
    mid = await create_merchant(client)
    r = await client.get(f"/api/merchants/{mid}")
    assert r.status_code == 200 and r.json()["merchant_id"] == mid


async def test_MER_05_get_unknown_merchant(client):
    r = await client.get(f"/api/merchants/{ZERO_ID}")
    assert r.status_code == 404 and r.json()["detail"] == "Merchant nahi mila."


async def test_MER_06_get_malformed_id(client):
    assert (await client.get("/api/merchants/not-a-uuid")).status_code == 422
