"""Shared helpers for the per-router API tests. Test names carry the case IDs used in API_TESTS.md."""

import pytest

ZERO_ID = "00000000-0000-0000-0000-000000000000"


async def create_merchant(client, **overrides) -> str:
    body = {"name": "Sharma General Store", "upi_vpa": "sharma@paytm", **overrides}
    r = await client.post("/api/merchants", json=body)
    assert r.status_code == 201, r.text
    return r.json()["merchant_id"]


async def create_customer(client, mid: str, name: str = "Ramesh Patil", **extra) -> str:
    r = await client.post(f"/api/merchants/{mid}/customers", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()["customer_id"]


async def give_credit(client, mid: str, cid: str, rupees: float, **extra) -> dict:
    r = await client.post(f"/api/merchants/{mid}/customers/{cid}/credit", json={"amount_rupees": rupees, **extra})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
async def mid(client) -> str:
    return await create_merchant(client)


@pytest.fixture
async def seeded(client) -> str:
    r = await client.post("/demo/seed")
    assert r.status_code == 201
    return r.json()["merchant_id"]


async def customer_named(client, mid: str, name: str) -> dict:
    customers = (await client.get(f"/api/merchants/{mid}/customers")).json()["customers"]
    return next(c for c in customers if c["name"] == name)
