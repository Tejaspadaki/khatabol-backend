"""Groq and Sarvam adapters against fake transports (no network)."""

import base64
import json
from types import SimpleNamespace

import groq
import httpx
import pytest

from app.agent.llm import GroqLLM, LLMUnavailable
from app.agent.tools import TOOL_SCHEMAS
from app.voice.sarvam import SarvamClient, SpeechUnavailable, for_speech


def _completion(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def _tc(id_, name, args: str):
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=args))


class FakeCompletions:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _llm(*results) -> tuple[GroqLLM, FakeCompletions]:
    completions = FakeCompletions(*results)
    return GroqLLM(SimpleNamespace(chat=SimpleNamespace(completions=completions))), completions


def _bad_request(message: str) -> groq.BadRequestError:
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return groq.BadRequestError(message, response=httpx.Response(400, request=request), body=None)


async def test_groq_tool_calls_are_parsed_and_tools_converted():
    llm, completions = _llm(_completion(None, [_tc("c1", "find_customer", '{"name_query": "ramesh"}'),
                                               _tc("c2", "get_balance", "{not json")]))
    turn = await llm.chat(system="sys", messages=[{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    req = completions.requests[0]
    assert req["messages"][0] == {"role": "system", "content": "sys"}
    assert req["tools"][0]["type"] == "function" and req["tools"][0]["function"]["name"] == "find_customer"
    assert "strict" not in json.dumps(req["tools"])
    assert turn.tool_calls[0].arguments == {"name_query": "ramesh"}
    assert turn.tool_calls[1].arguments is None  # invalid JSON is reported back to the model, not executed
    assert turn.message["tool_calls"][0]["function"]["arguments"] == '{"name_query": "ramesh"}'


async def test_groq_retries_tool_use_failed_once():
    llm, completions = _llm(_bad_request("tool_use_failed: bad call"), _completion("theek hai"))
    turn = await llm.chat(system="s", messages=[], tools=[])
    assert turn.text == "theek hai" and len(completions.requests) == 2


async def test_groq_errors_become_unavailable():
    llm, _ = _llm(_bad_request("model_not_found"))
    with pytest.raises(LLMUnavailable):
        await llm.chat(system="s", messages=[], tools=[])


async def test_groq_without_key():
    llm = GroqLLM()
    llm._api_key = ""
    with pytest.raises(LLMUnavailable, match="GROQ_API_KEY"):
        await llm.chat(system="s", messages=[], tools=[])


def _sarvam(handler) -> SarvamClient:
    client = SarvamClient(httpx.AsyncClient(base_url="https://api.sarvam.ai", transport=httpx.MockTransport(handler)))
    client.s = client.s.model_copy(update={"sarvam_api_key": "test-key"})
    return client


async def test_sarvam_stt_request_shape():
    seen = {}

    def handler(request: httpx.Request):
        seen["path"], seen["key"] = request.url.path, request.headers["api-subscription-key"]
        seen["body"] = request.content
        return httpx.Response(200, json={"request_id": "r1", "transcript": " Ramesh ko 500 ", "language_code": "hi-IN"})

    t = await _sarvam(handler).transcribe(b"audio", "clip.webm", "audio/webm", "hi")
    assert t.text == "Ramesh ko 500" and seen["path"] == "/speech-to-text" and seen["key"] == "test-key"
    for field in (b'name="model"', b"saaras:v3", b'name="mode"', b"translit", b"hi-IN"):
        assert field in seen["body"]


async def test_sarvam_tts_decodes_audio_and_speaks_rupees():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"request_id": "r2", "audios": [base64.b64encode(b"WAVDATA").decode()]})

    wav = await _sarvam(handler).synthesize("Ab ₹1,500 baaki", "hi")
    assert wav == b"WAVDATA"
    assert seen["text"] == "Ab 1,500 rupaye baaki"
    assert seen["language_code"] == "hi-IN" and seen["model"] == "bulbul:v3" and seen["speaker"] == "priya"


async def test_sarvam_errors():
    client = _sarvam(lambda r: httpx.Response(403, json={"error": {"message": "bad key"}}))
    with pytest.raises(SpeechUnavailable, match="403"):
        await client.synthesize("hi")


def test_for_speech():
    assert for_speech("₹350 udhaar, ab ₹12,34,567 baaki") == "350 rupaye udhaar, ab 12,34,567 rupaye baaki"


# --- Gemma 4 (OpenAI-compatible endpoint) ---

from app.agent.llm import Gemma4LLM, make_llm  # noqa: E402


def _gemma(handler) -> Gemma4LLM:
    client = httpx.AsyncClient(base_url="https://gemma.test/v1", transport=httpx.MockTransport(handler))
    return Gemma4LLM(client)


async def test_gemma_tool_calls_are_parsed():
    seen = {}

    def handler(request: httpx.Request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "find_customer", "arguments": '{"name_query": "ramesh"}'}},
            {"id": "c2", "type": "function", "function": {"name": "get_balance", "arguments": {"customer": "x"}}},
        ]}}]})

    turn = await _gemma(handler).chat(system="sys", messages=[{"role": "user", "content": "hi"}], tools=TOOL_SCHEMAS)
    assert seen["path"] == "/v1/chat/completions"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["tools"][0]["function"]["name"] == "find_customer"
    assert turn.tool_calls[0].arguments == {"name_query": "ramesh"}
    assert turn.tool_calls[1].arguments == {"customer": "x"}  # dict arguments are accepted too
    assert turn.message["tool_calls"][0]["function"]["name"] == "find_customer"


async def test_gemma_text_reply_strips_think():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>hmm</think>theek hai"}}]})

    turn = await _gemma(handler).chat(system="s", messages=[], tools=[])
    assert turn.text == "theek hai" and turn.tool_calls == []


async def test_gemma_retries_5xx_once_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    turn = await _gemma(handler).chat(system="s", messages=[], tools=[])
    assert turn.text == "ok" and len(calls) == 2


async def test_gemma_errors_become_unavailable():
    with pytest.raises(LLMUnavailable, match="401"):
        await _gemma(lambda r: httpx.Response(401, json={"detail": "Missing API key"})).chat(
            system="s", messages=[], tools=[])
    with pytest.raises(LLMUnavailable) as exc:
        await _gemma(lambda r: httpx.Response(429, text="slow down")).chat(system="s", messages=[], tools=[])
    assert exc.value.rate_limited


async def test_gemma_without_key():
    llm = Gemma4LLM()
    llm._api_key = ""
    with pytest.raises(LLMUnavailable, match="GEMMA4_API_KEY"):
        await llm.chat(system="s", messages=[], tools=[])


def test_make_llm_follows_provider(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "gemma4")
    assert isinstance(make_llm(), Gemma4LLM)
    monkeypatch.setattr(s, "llm_provider", "groq")
    assert isinstance(make_llm(), GroqLLM)
