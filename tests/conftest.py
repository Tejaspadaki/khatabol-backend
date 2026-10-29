import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.llm import LLMTurn, ToolCall
from app.config import get_settings
from app.db import base
from app.ledger import service as ledger
from app.voice.sarvam import SpeechUnavailable, Transcript


@pytest.fixture
async def db(tmp_path):
    base.init_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await base.create_all()
    yield
    await base._engine.dispose()


@pytest.fixture
async def session(db):
    async with base.get_sessionmaker()() as s:
        yield s


@pytest.fixture
async def merchant(session):
    m = await ledger.create_merchant(session, "Sharma General Store", upi_vpa="sharma@paytm")
    await session.commit()
    return m


@pytest.fixture
async def client(db, tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setattr(get_settings(), "voice_clip_dir", str(tmp_path / "clips"))
    app.state.sarvam = FakeSarvam()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.state.orchestrator = None
    app.state.sarvam = None


# ---------------------------------------------------------------- fakes

def text(t: str):
    return SimpleNamespace(type="text", text=t)


_ids = iter(range(1, 10**6))


def tool(tool_name: str, /, **inp):
    return SimpleNamespace(type="tool_use", id=f"call_{next(_ids)}", name=tool_name, input=inp)


def last_results(messages: list[dict]) -> list[dict]:
    """Parsed tool results that follow the most recent assistant message."""
    out = []
    for m in reversed(messages):
        if m["role"] != "tool":
            break
        body = m["content"]
        out.insert(0, {"error": body} if body.startswith("Error:") else json.loads(body))
    return out


class FakeLLM:
    """Replays a script. Each step is a list of blocks, or a callable(messages) -> list of blocks."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[list[dict]] = []

    async def chat(self, *, system, messages, tools):
        self.calls.append(list(messages))
        step = self.steps.pop(0)
        blocks = step(messages) if callable(step) else step
        calls = [ToolCall(b.id, b.name, b.input, json.dumps(b.input)) for b in blocks if b.type == "tool_use"]
        reply = " ".join(b.text for b in blocks if b.type == "text")
        message = {"role": "assistant", "content": reply}
        if calls:
            message["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": c.raw_arguments}} for c in calls]
        return LLMTurn(reply, calls, message)


class FakeSarvam:
    def __init__(self, transcript: str = "", fail: bool = False):
        self.transcript, self.fail = transcript, fail
        self.spoken: list[str] = []

    async def transcribe(self, audio, filename, content_type, language="hi"):
        if self.fail:
            raise SpeechUnavailable("down")
        return Transcript(self.transcript, "hi-IN")

    async def synthesize(self, text, language="hi"):
        if self.fail:
            raise SpeechUnavailable("down")
        self.spoken.append(text)
        return b"RIFF....WAVEfake"
