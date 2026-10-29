"""AGT: /agent/message, /agent/voice, /tts (fake Groq + fake Sarvam; no network)."""

import base64
from pathlib import Path

from app.agent.llm import LLMUnavailable
from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.main import app
from tests.api.conftest import ZERO_ID, create_customer, give_credit
from tests.conftest import FakeLLM, FakeSarvam, text, tool


def use_llm(*steps) -> FakeLLM:
    llm = FakeLLM(*steps)
    app.state.orchestrator = Orchestrator(llm)
    return llm


def credit(amount, due=None, customer="Ramesh Patil"):
    args = {"customer": customer, "amount_rupees": amount}
    if due:
        args["due"] = due
    return [tool("add_credit", **args)]


async def say(client, mid, text_, session="s1", **extra):
    r = await client.post(f"/api/merchants/{mid}/agent/message", json={"text": text_, "session_id": session, **extra})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- /agent/message

async def test_AGT_01_plain_answer(client, mid):
    use_llm([text("Namaste! Bataiye kya likhna hai?")])
    body = await say(client, mid, "namaste")
    assert body["reply"] == "Namaste! Bataiye kya likhna hai?" and body["outcome"] == "answered"
    assert body["session_id"] == "s1" and body["pending"] == [] and body["latency_ms"] >= 0
    assert "audio_base64" not in body


async def test_AGT_02_credit_needs_haan(client, mid):
    cid = await create_customer(client, mid, "Ramesh Patil")
    await give_credit(client, mid, cid, 500)
    llm = use_llm(credit(350, "somvar"))
    body = await say(client, mid, "Ramesh Patil ko saadhe teen sau ka saamaan, somvar tak")
    assert body["outcome"] == "awaiting_confirmation"
    assert body["pending"][0]["tool"] == "add_credit" and "₹350" in body["pending"][0]["summary"]
    assert [c["name"] for c in body["tool_calls"]] == ["add_credit"]
    assert body["reply"].endswith("Save karun?")
    detail = (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()
    assert detail["balance"] == "₹500"  # nothing written yet

    body = await say(client, mid, "haan")
    assert body["outcome"] == "committed" and body["reply"] == "Ho gaya. Ramesh Patil: ab ₹850 baaki."
    assert len(llm.calls) == 1  # one LLM call for the proposal; "haan" never reaches the LLM
    assert (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()["balance"] == "₹850"
    entry = (await client.get(f"/api/merchants/{mid}/customers/{cid}/history")).json()["entries"][0]
    assert entry["source"] == "VOICE" and entry["amount"] == "₹350"


async def test_AGT_03_nahi_cancels(client, mid):
    cid = await create_customer(client, mid, "Ramesh Patil")
    use_llm(credit(200))
    await say(client, mid, "Ramesh Patil ko 200")
    body = await say(client, mid, "nahi")
    assert body["outcome"] == "cancelled" and body["reply"] == "Theek hai, cancel kar diya."
    assert (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()["balance"] == "₹0"


async def test_AGT_04_sessions_are_separate(client, mid):
    cid = await create_customer(client, mid, "Ramesh Patil")
    use_llm(credit(200), [text("Kis baat ke liye haan?")])
    await say(client, mid, "Ramesh Patil ko 200", session="counter-1")
    other = await say(client, mid, "haan", session="counter-2")  # no pending in this session -> LLM
    assert other["outcome"] == "answered"
    assert (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()["balance"] == "₹0"


async def test_AGT_05_message_validation(client, mid):
    url = f"/api/merchants/{mid}/agent/message"
    assert (await client.post(url, json={})).status_code == 422
    assert (await client.post(url, json={"text": ""})).status_code == 422
    assert (await client.post(url, json={"text": "x" * 1001})).status_code == 422
    assert (await client.post(url, json={"text": "hi", "session_id": "s" * 65})).status_code == 422


async def test_AGT_06_message_unknown_merchant(client):
    use_llm()
    r = await client.post(f"/api/merchants/{ZERO_ID}/agent/message", json={"text": "hi"})
    assert r.status_code == 404


async def test_AGT_07_llm_down_gives_spoken_fallback(client, mid):
    class Down:
        async def chat(self, **_):
            raise LLMUnavailable("GROQ_API_KEY is not set.")

    app.state.orchestrator = Orchestrator(Down())
    body = await say(client, mid, "Ramesh ka hisaab")
    assert body["outcome"] == "llm_unavailable" and "app se entry" in body["reply"]


async def test_AGT_08_speak_returns_tts_audio(client, mid):
    use_llm([text("Kul ₹2,400 baaki hai.")])
    body = await say(client, mid, "kul kitna baaki", speak=True)
    assert body["audio_mime"] == "audio/wav" and base64.b64decode(body["audio_base64"]).startswith(b"RIFF")
    assert app.state.sarvam.spoken == ["Kul ₹2,400 baaki hai."]


async def test_AGT_09_speak_with_tts_down_still_replies(client, mid):
    app.state.sarvam = FakeSarvam(fail=True)
    use_llm([text("Theek hai.")])
    body = await say(client, mid, "ok", speak=True)
    assert body["reply"] == "Theek hai." and body["audio_base64"] is None and body["tts_error"]


# ---------------------------------------------------------------- /agent/voice

def clip(data: bytes = b"x" * 3000, name: str = "clip.webm", mime: str = "audio/webm"):
    return {"audio": (name, data, mime)}


async def test_AGT_10_voice_turn(client, mid):
    app.state.sarvam = FakeSarvam(transcript="namaste")
    use_llm([text("Namaste ji.")])
    r = await client.post(f"/api/merchants/{mid}/agent/voice", data={"session_id": "v1"}, files=clip())
    body = r.json()
    assert r.status_code == 200
    assert body["transcript"] == "namaste" and body["stt_language"] == "hi-IN"
    assert body["reply"] == "Namaste ji." and body["session_id"] == "v1" and body["audio_base64"]
    clips = list(Path(get_settings().voice_clip_dir).iterdir())
    assert len(clips) == 1 and clips[0].suffix == ".webm" and clips[0].read_bytes() == b"x" * 3000


async def test_AGT_11_voice_turn_confirms_with_haan(client, mid):
    cid = await create_customer(client, mid, "Ramesh Patil")
    use_llm(credit(100))
    app.state.sarvam = FakeSarvam(transcript="Ramesh Patil ko sau rupaye")
    await client.post(f"/api/merchants/{mid}/agent/voice", data={"session_id": "v"}, files=clip())
    app.state.sarvam = FakeSarvam(transcript="haan")
    body = (await client.post(f"/api/merchants/{mid}/agent/voice", data={"session_id": "v"}, files=clip())).json()
    assert body["outcome"] == "committed"
    assert (await client.get(f"/api/merchants/{mid}/customers/{cid}")).json()["balance"] == "₹100"


async def test_AGT_12_voice_empty_transcript(client, mid):
    app.state.sarvam = FakeSarvam(transcript="")
    llm = use_llm()
    body = (await client.post(f"/api/merchants/{mid}/agent/voice", files=clip())).json()
    assert body["reply"] == "Phir se boliye?" and body["outcome"] == "low_confidence" and not llm.calls


async def test_AGT_13_voice_bad_uploads(client, mid):
    url = f"/api/merchants/{mid}/agent/voice"
    assert (await client.post(url, files=clip(b""))).status_code == 400
    assert (await client.post(url, files=clip(b"x" * (10 * 1024 * 1024 + 1)))).status_code == 413
    assert (await client.post(url)).status_code == 422  # no audio field


async def test_AGT_14_voice_stt_down(client, mid):
    app.state.sarvam = FakeSarvam(fail=True)
    r = await client.post(f"/api/merchants/{mid}/agent/voice", files=clip())
    assert r.status_code == 503 and r.json()["detail"].startswith("Speech-to-text unavailable")


async def test_AGT_15_voice_unknown_merchant(client):
    assert (await client.post(f"/api/merchants/{ZERO_ID}/agent/voice", files=clip())).status_code == 404


# ---------------------------------------------------------------- /tts

async def test_AGT_16_tts(client, mid):
    r = await client.post(f"/api/merchants/{mid}/tts", json={"text": "Ramesh ne ₹300 diye"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content.startswith(b"RIFF")


async def test_AGT_17_tts_validation_and_errors(client, mid):
    assert (await client.post(f"/api/merchants/{mid}/tts", json={"text": ""})).status_code == 422
    assert (await client.post(f"/api/merchants/{mid}/tts", json={"text": "x" * 2001})).status_code == 422
    assert (await client.post(f"/api/merchants/{ZERO_ID}/tts", json={"text": "hi"})).status_code == 404
    app.state.sarvam = FakeSarvam(fail=True)
    assert (await client.post(f"/api/merchants/{mid}/tts", json={"text": "hi"})).status_code == 503
