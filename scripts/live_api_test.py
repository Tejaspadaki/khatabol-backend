"""Live end-to-end test of every API endpoint against a running server, with real Groq + Sarvam.

    uv run uvicorn app.main:app --port 8000          # in one terminal (keys in .env)
    uv run python scripts/live_api_test.py           # in another

Writes LIVE_TEST_RESULTS.md next to this repo's API_TESTS.md. Uses a fresh demo shop each run.
Cases marked [AI] depend on the LLM / speech models, so their wording can vary between runs;
they check structure and the khata effect, not exact sentences.
"""

import argparse
import asyncio
import base64
import json
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import websockets

ZERO = "00000000-0000-0000-0000-000000000000"


@dataclass
class Result:
    case: str
    endpoint: str
    check: str
    ok: bool
    ms: int
    detail: str = ""


class Runner:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base, timeout=60)
        self.results: list[Result] = []
        self.ctx: dict = {}

    async def case(self, case: str, endpoint: str, check: str, fn):
        start = time.perf_counter()
        try:
            detail = await fn() or ""
            ok = True
        except AssertionError as exc:
            ok, detail = False, f"assertion failed: {exc}"
        except Exception as exc:  # report and keep going
            ok, detail = False, f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        ms = int((time.perf_counter() - start) * 1000)
        self.results.append(Result(case, endpoint, check, ok, ms, str(detail)))
        print(f"{'PASS' if ok else 'FAIL'}  {case:12} {ms:6} ms  {endpoint}  {detail}"[:220])

    # helpers
    async def get(self, path, **kw):
        return await self.http.get(path, **kw)

    async def post(self, path, **kw):
        return await self.http.post(path, **kw)

    def m(self, path=""):
        return f"/api/merchants/{self.ctx['mid']}{path}"

    async def customer(self, name):
        customers = (await self.get(self.m("/customers"))).json()["customers"]
        return next(c for c in customers if c["name"] == name)


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


async def run(r: Runner):
    session = f"live-{uuid.uuid4().hex[:6]}"

    # ---------------- system / demo
    async def sys01():
        res = await r.get("/health")
        expect(res.status_code == 200 and res.json() == {"ok": True}, res.text)
    await r.case("SYS-01", "GET /health", "200 {ok:true}", sys01)

    async def sys02():
        res = await r.http.get("/", follow_redirects=False)
        expect(res.status_code == 307 and res.headers["location"] == "/app", res.status_code)
    await r.case("SYS-02", "GET /", "redirects to app", sys02)

    async def dem01():
        res = await r.post("/demo/seed")
        expect(res.status_code == 201, res.text)
        r.ctx["mid"] = res.json()["merchant_id"]
        return f"merchant {r.ctx['mid'][:8]}"
    await r.case("DEM-01", "POST /demo/seed", "201, demo shop created", dem01)
    if "mid" not in r.ctx:
        return

    async def dem02():
        ids = [m["merchant_id"] for m in (await r.get("/demo/merchants")).json()["merchants"]]
        expect(ids[0] == r.ctx["mid"], "new shop not listed first")
    await r.case("DEM-02", "GET /demo/merchants", "newest first", dem02)

    async def dem03():
        res = await r.get("/demo/simulate")
        expect(res.status_code == 200 and "Hold to talk" in res.text, res.status_code)
    await r.case("DEM-03", "GET /demo/simulate", "HTML console", dem03)

    # ---------------- merchants
    async def mer01():
        res = await r.post("/api/merchants", json={"name": "Live Test Store", "upi_vpa": "live@paytm"})
        expect(res.status_code == 201, res.text)
        r.ctx["mid2"] = res.json()["merchant_id"]
    await r.case("MER-01", "POST /api/merchants", "201", mer01)

    async def mer04():
        res = await r.get(f"/api/merchants/{r.ctx['mid2']}")
        expect(res.status_code == 200 and res.json()["name"] == "Live Test Store", res.text)
    await r.case("MER-04", "GET /api/merchants/{m}", "200", mer04)

    async def mer05():
        res = await r.get(f"/api/merchants/{ZERO}")
        expect(res.status_code == 404, res.status_code)
    await r.case("MER-05", "GET /api/merchants/{m}", "404 unknown", mer05)

    # ---------------- customers
    async def cus01():
        res = await r.post(r.m("/customers"), json={"name": "kavita joshi", "phone": "9000011111"})
        expect(res.status_code == 201 and res.json()["name"] == "Kavita Joshi", res.text)
        r.ctx["kavita"] = res.json()["customer_id"]
    await r.case("CUS-01", "POST .../customers", "201, name normalized", cus01)

    async def cus05():
        names = [c["name"] for c in (await r.get(r.m("/customers"))).json()["customers"]]
        expect(len(names) == 6 and names == sorted(names), names)
    await r.case("CUS-05", "GET .../customers", "6 customers, sorted", cus05)

    async def cus06():
        res = (await r.get(r.m("/customers"), params={"q": "ramesh bhaiya"})).json()
        expect(res["decision"] == "ambiguous", res["decision"])
        res = (await r.get(r.m("/customers"), params={"q": "sunitha"})).json()
        expect(res["decision"] == "match" and res["customers"][0]["name"] == "Sunita Devi", res)
    await r.case("CUS-06", "GET .../customers?q=", "ambiguous / match", cus06)

    async def cus13():
        res = await r.post(r.m(f"/customers/{r.ctx['kavita']}/credit"),
                           json={"amount_rupees": 500, "due": "5 tareekh", "items": "aata"})
        expect(res.status_code == 201 and res.json()["customer"]["balance"] == "₹500", res.text)
        return f"due {res.json()['entry']['due_date']}"
    await r.case("CUS-13", "POST .../credit", "spoken due resolved", cus13)

    async def cus14():
        res = await r.post(r.m(f"/customers/{r.ctx['kavita']}/credit"), json={"amount_rupees": 5, "due": "jab honge"})
        expect(res.status_code == 400, res.status_code)
    await r.case("CUS-14", "POST .../credit", "400 bad due", cus14)

    async def cus17():
        res = await r.post(r.m(f"/customers/{r.ctx['kavita']}/payment"), json={"amount_rupees": 200, "mode": "upi"})
        expect(res.json()["customer"]["balance"] == "₹300", res.text)
        r.ctx["kavita_pay"] = res.json()["entry"]["entry_id"]
    await r.case("CUS-17", "POST .../payment", "balance ₹300", cus17)

    async def cus19():
        res = await r.post(r.m(f"/customers/{r.ctx['kavita']}/upi"), json={"vpa": "Kavita@YBL"})
        expect(res.status_code == 201 and res.json()["vpa"] == "kavita@ybl", res.text)
    await r.case("CUS-19", "POST .../upi", "VPA linked", cus19)

    async def cus08():
        d = (await r.get(r.m(f"/customers/{r.ctx['kavita']}"))).json()
        expect(d["balance"] == "₹300" and d["upi_ids"] == ["kavita@ybl"] and d["next_due"] == "₹300", d)
    await r.case("CUS-08", "GET .../customers/{c}", "balance, due, UPI", cus08)

    async def cus11():
        e = (await r.get(r.m(f"/customers/{r.ctx['kavita']}/history"))).json()["entries"]
        expect([x["type"] for x in e] == ["PAYMENT", "CREDIT"], e)
    await r.case("CUS-11", "GET .../history", "newest first", cus11)

    async def cus22():
        res = await r.post(r.m(f"/entries/{r.ctx['kavita_pay']}/reverse"))
        expect(res.status_code == 201 and res.json()["customer"]["balance"] == "₹500", res.text)
        again = await r.post(r.m(f"/entries/{r.ctx['kavita_pay']}/reverse"))
        expect(again.status_code == 400, again.status_code)
    await r.case("CUS-22", "POST .../entries/{e}/reverse", "reversed once only", cus22)

    async def cus10():
        res = await r.get(f"/api/merchants/{r.ctx['mid2']}/customers/{r.ctx['kavita']}")
        expect(res.status_code == 404, res.status_code)
    await r.case("CUS-10", "GET .../customers/{c}", "other shop -> 404", cus10)

    # ---------------- collections
    async def col01():
        body = (await r.get(r.m("/dues"), params={"filter": "today"})).json()
        names = {c["name"] for c in body["customers"]}
        expect(names == {"Ramesh Patil", "Sunita Devi"}, names)
        return f"total {body['total_due']}"
    await r.case("COL-01", "GET .../dues?filter=today", "Ramesh Patil + Sunita", col01)

    async def col05():
        body = (await r.post(r.m("/reminders"), json={"all_due": True})).json()
        expect(body["sent"] == 2 and body["reminders"][0]["payment_link"].startswith("upi://pay?"), body)
    await r.case("COL-05", "POST .../reminders", "2 sent with UPI links", col05)

    async def col08():
        res = await r.post(r.m("/reminders"), json={})
        expect(res.status_code == 422, res.status_code)
    await r.case("COL-08", "POST .../reminders", "422 no target", col08)

    # ---------------- payments + live events
    async def pay01_ws01():
        ws_url = r.base.replace("http", "ws", 1) + f"/ws/merchants/{r.ctx['mid']}/events"
        async with websockets.connect(ws_url) as ws:
            body = (await r.post(r.m("/payments/incoming"),
                                 json={"payer_vpa": "ramesh.p@okaxis", "amount_rupees": 300, "txn_id": f"LIVE-{session}"})).json()
            expect(body["status"] == "APPLIED", body)
            ev = json.loads(await asyncio.wait_for(ws.recv(), 5))
            expect(ev["type"] == "upi_applied" and ev["text"] == body["announcement"], ev)
        return body["announcement"]
    await r.case("PAY-01/WS-01", "POST .../payments/incoming + WS", "auto-settle + live event", pay01_ws01)

    async def pay02():
        body = (await r.post(r.m("/payments/incoming"),
                             json={"payer_vpa": "ramesh.p@okaxis", "amount_rupees": 300, "txn_id": f"LIVE-{session}"})).json()
        expect(body["duplicate"] is True, body)
        expect((await r.customer("Ramesh Patil"))["balance"] == "₹200", "double counted")
    await r.case("PAY-02", "POST .../payments/incoming", "duplicate ignored", pay02)

    async def pay04():
        body = (await r.post(r.m("/payments/incoming"),
                             json={"payer_vpa": "sunita99@ybl", "amount_rupees": 200, "txn_id": f"UNK-{session}"})).json()
        expect(body["status"] == "UNMATCHED", body)
        return body["announcement"]
    await r.case("PAY-04", "POST .../payments/incoming", "unknown payer asked", pay04)

    async def pay10():
        txns = [p["txn_id"] for p in (await r.get(r.m("/payments/unmatched"))).json()["payments"]]
        expect(txns == [f"UNK-{session}"], txns)
    await r.case("PAY-10", "GET .../payments/unmatched", "1 waiting", pay10)

    async def pay11():
        sunita = await r.customer("Sunita Devi")
        body = (await r.post(r.m(f"/payments/UNK-{session}/assign"), json={"customer_id": sunita["customer_id"]})).json()
        expect(body["status"] == "RESOLVED" and body["customer"]["balance"] == "₹250", body)
        return body["spoken"]
    await r.case("PAY-11", "POST .../payments/{t}/assign", "resolved, VPA learned", pay11)

    async def pay13():
        body = (await r.post(r.m(f"/payments/LIVE-{session}/undo"))).json()
        expect(body["status"] == "UNMATCHED", body)
        expect((await r.customer("Ramesh Patil"))["balance"] == "₹500", "not restored")
    await r.case("PAY-13", "POST .../payments/{t}/undo", "reversed, back to queue", pay13)

    async def col10():
        s = (await r.get(r.m("/summary"))).json()
        expect(s["spoken"].startswith("Aaj"), s)
        return s["spoken"]
    await r.case("COL-10", "GET .../summary", "spoken summary", col10)

    # ---------------- AI: Sarvam TTS / STT, Groq agent
    async def tts():
        res = await r.post(r.m("/tts"), json={"text": "Ramesh Patil ne ₹300 UPI se diye. Ab ₹200 baaki."})
        expect(res.status_code == 200 and res.content[:4] == b"RIFF", f"{res.status_code} {res.text[:200]}")
        r.ctx["wav_bytes"] = len(res.content)
        return f"{len(res.content)} bytes WAV"
    await r.case("AGT-16 [AI]", "POST .../tts", "Sarvam bulbul WAV", tts)

    async def agent_q():
        body = (await r.post(r.m("/agent/message"), json={"text": "Ramesh Patil ka kitna baaki hai?", "session_id": session})).json()
        expect(body["outcome"] == "answered", body)
        tools = [c["name"] for c in body["tool_calls"]]
        expect("get_balance" in tools, tools)
        expect("500" in body["reply"], f"reply lacks ₹500: {body['reply']}")
        return f"{body['reply']}  (tools: {', '.join(tools)})"
    await r.case("AGT-01 [AI]", "POST .../agent/message", "balance answered from tools", agent_q)

    async def agent_credit():
        mohan = await r.customer("Mohan Lal")
        body = (await r.post(r.m("/agent/message"),
                             json={"text": "Mohan Lal ko 120 rupaye ka saamaan diya, kal tak dega", "session_id": session})).json()
        expect(body["outcome"] == "awaiting_confirmation", body)
        pend = body["pending"]
        expect(len(pend) == 1 and pend[0]["tool"] == "add_credit" and "₹120" in pend[0]["summary"], pend)
        expect((await r.customer("Mohan Lal"))["balance"] == mohan["balance"], "written before haan")
        r.ctx["mohan_before"] = mohan["balance_paise"]
        return f"{body['reply']}"
    await r.case("AGT-02a [AI]", "POST .../agent/message", "credit proposed, not saved", agent_credit)

    async def agent_haan():
        body = (await r.post(r.m("/agent/message"), json={"text": "haan", "session_id": session})).json()
        expect(body["outcome"] == "committed", body)
        expect((await r.customer("Mohan Lal"))["balance_paise"] == r.ctx["mohan_before"] + 12000, "balance")
        return body["reply"]
    await r.case("AGT-02b", "POST .../agent/message", "'haan' commits (no LLM)", agent_haan)

    async def agent_multi():
        body = (await r.post(r.m("/agent/message"), json={
            "text": "Ramesh Sharma ko 100 ka saamaan diya aur Sunita Devi ne 50 cash lautaye", "session_id": session,
        })).json()
        tools = sorted(p["tool"] for p in body["pending"])
        expect(tools == ["add_credit", "record_payment"], body)
        body2 = (await r.post(r.m("/agent/message"), json={"text": "nahi", "session_id": session})).json()
        expect(body2["outcome"] == "cancelled", body2)
        return body["reply"]
    await r.case("AGT-03 [AI]", "POST .../agent/message", "multi-intent, then 'nahi'", agent_multi)

    async def agent_speak():
        body = (await r.post(r.m("/agent/message"),
                             json={"text": "aaj kiska paisa aana hai?", "session_id": session, "speak": True})).json()
        expect(body["outcome"] == "answered" and body.get("audio_base64"), body.get("tts_error") or body)
        return f"{body['reply']}  (+{len(base64.b64decode(body['audio_base64']))} bytes audio)"
    await r.case("AGT-08 [AI]", "POST .../agent/message speak", "reply + Sarvam audio", agent_speak)

    async def voice():
        # Round trip: Sarvam TTS makes the "merchant's" voice, Sarvam STT hears it, Groq answers.
        wav = (await r.post(r.m("/tts"), json={"text": "Sunita Devi ka kitna baaki hai?"})).content
        res = await r.post(r.m("/agent/voice"), data={"session_id": session + "-v"},
                           files={"audio": ("question.wav", wav, "audio/wav")})
        body = res.json()
        expect(res.status_code == 200, body)
        expect("sunita" in body["transcript"].lower(), f"transcript: {body['transcript']}")
        expect(body["outcome"] == "answered" and body.get("audio_base64"), body)
        return f"heard '{body['transcript']}' -> '{body['reply']}'"
    await r.case("AGT-10 [AI]", "POST .../agent/voice", "STT -> agent -> TTS", voice)

    async def voice_bad():
        res = await r.post(r.m("/agent/voice"), files={"audio": ("e.webm", b"", "audio/webm")})
        expect(res.status_code == 400, res.status_code)
    await r.case("AGT-13", "POST .../agent/voice", "400 empty clip", voice_bad)


def write_report(results: list[Result], base: str, path: Path):
    passed = sum(r.ok for r in results)
    lines = [
        "# Live API test results",
        "",
        f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')} against `{base}` with real Groq + Sarvam.",
        f"Produced by `scripts/live_api_test.py`; case IDs match [API_TESTS.md](API_TESTS.md).",
        "",
        f"**{passed} / {len(results)} passed.**",
        "",
        "| Case | Endpoint | Check | Result | Time | Detail |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        detail = r.detail.replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| {r.case} | `{r.endpoint}` | {r.check} | {'PASS' if r.ok else '**FAIL**'} | {r.ms} ms | {detail} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "LIVE_TEST_RESULTS.md"))
    args = ap.parse_args()
    runner = Runner(args.base)
    try:
        await run(runner)
    finally:
        await runner.http.aclose()
    write_report(runner.results, args.base, Path(args.out))
    failed = [r for r in runner.results if not r.ok]
    print(f"\n{len(runner.results) - len(failed)}/{len(runner.results)} passed -> {args.out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
