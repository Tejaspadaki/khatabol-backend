"""SYS / DEM / WS: health, root redirect, demo routes, voice clip files, live events WebSocket."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import base
from app.main import app


async def test_SYS_01_health(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_SYS_02_root_redirects_to_app(client):
    r = await client.get("/")
    assert r.status_code == 307 and r.headers["location"] == "/app"


async def test_SYS_03_openapi_lists_every_route(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert len(paths) == 24  # 25 operations; /customers has GET and POST
    assert "/api/merchants/{merchant_id}/payments/incoming" in paths
    assert not any("webhook" in p for p in paths)


async def test_SYS_04_voice_clip_is_served(client):
    clip_dir = Path(next(r for r in app.routes if getattr(r, "name", "") == "voice-clips").app.directory)
    name = f"test-{uuid.uuid4().hex}.webm"
    (clip_dir / name).write_bytes(b"clipdata")
    try:
        r = await client.get(f"/voice-clips/{name}")
        assert r.status_code == 200 and r.content == b"clipdata"
    finally:
        (clip_dir / name).unlink()
    assert (await client.get("/voice-clips/missing.webm")).status_code == 404


async def test_DEM_01_seed(client):
    r = await client.post("/demo/seed")
    assert r.status_code == 201 and r.json()["name"] == "Sharma General Store"
    customers = (await client.get(f"/api/merchants/{r.json()['merchant_id']}/customers")).json()["customers"]
    assert len(customers) == 5


async def test_DEM_02_list_merchants_newest_first(client):
    await client.post("/api/merchants", json={"name": "First"})
    await client.post("/api/merchants", json={"name": "Second"})
    names = [m["name"] for m in (await client.get("/demo/merchants")).json()["merchants"]]
    assert names == ["Second", "First"]


async def test_DEM_03_demo_page(client):
    r = await client.get("/demo/simulate")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Hold to talk" in r.text and "/payments/incoming" in r.text


def test_WS_01_events_stream(tmp_path):
    """Sync TestClient so the app, DB and event bus share one event loop."""
    base.init_engine(f"sqlite+aiosqlite:///{tmp_path / 'ws.db'}")
    with TestClient(app) as tc:  # lifespan creates the tables
        mid = tc.post("/demo/seed").json()["merchant_id"]
        with tc.websocket_connect(f"/ws/merchants/{mid}/events") as ws:
            tc.post(f"/api/merchants/{mid}/payments/incoming", json={"payer_vpa": "ramesh.p@okaxis", "amount_rupees": 300})
            ev = ws.receive_json()
            assert ev["type"] == "upi_applied" and ev["text"] == "Ramesh Patil ne ₹300 UPI se diye. Ab ₹200 baaki."
            assert ev["amount_paise"] == 30000 and ev["status"] == "APPLIED" and "at" in ev

            tc.post(f"/api/merchants/{mid}/payments/incoming", json={"payer_vpa": "new@ybl", "amount_rupees": 20})
            ev = ws.receive_json()
            assert ev["type"] == "upi_unmatched" and ev["payer_vpa"] == "new@ybl"
        tc.portal.call(base._engine.dispose)


async def test_DEM_04_tester_page(client):
    r = await client.get("/demo/tester")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Khata Voice API Tester" in r.text
    assert 'name="color-scheme" content="light"' in r.text and "prefers-color-scheme" not in r.text
    # every REST path in the app appears in the tester's catalog
    paths = (await client.get("/openapi.json")).json()["paths"]
    for p in paths:
        if p.startswith("/api/") or p in ("/health", "/demo/seed", "/demo/merchants"):
            assert f'"{p}"' in r.text, p


async def test_APP_01_merchant_app(client):
    r = await client.get("/app")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'name="color-scheme" content="light"' in r.text and "prefers-color-scheme" not in r.text
    for used in ("/agent/voice", "/agent/message", "/tts", "/payments/incoming", "/payments/unmatched",
                 "/dues?filter=today", "/summary", "/reminders", "/ws/merchants/"):
        assert used in r.text, used


async def test_APP_02_no_page_uses_dark_theme(client):
    for page in ("/app", "/demo/simulate", "/demo/tester"):
        assert "prefers-color-scheme: dark" not in (await client.get(page)).text, page
