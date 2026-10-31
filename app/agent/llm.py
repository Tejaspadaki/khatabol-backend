"""LLM behind one small interface so the orchestrator is testable and the vendor is swappable.

Messages use the OpenAI-style chat format (user / assistant with tool_calls / tool), which Groq speaks.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import groq
import httpx
from groq import AsyncGroq

from app.config import get_settings

log = logging.getLogger("khata.llm")
_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)


class LLMUnavailable(Exception):
    """The LLM could not be reached (no key, network, rate limit after retries, 5xx)."""

    def __init__(self, message: str, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict | None  # None when the model produced invalid JSON
    raw_arguments: str = ""


@dataclass
class LLMTurn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    message: dict = field(default_factory=dict)  # assistant message to append to history


class LLMClient(Protocol):
    async def chat(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn: ...


def openai_tools(schemas: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                          "parameters": t["input_schema"]}}
        for t in schemas
    ]


def _parse_args(raw: str | None) -> dict | None:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class GroqLLM:
    def __init__(self, client: AsyncGroq | None = None) -> None:
        s = get_settings()
        self.model = s.llm_model
        self.temperature = s.llm_temperature
        self.max_tokens = s.llm_max_tokens
        self.reasoning = {k: v for k, v in (("reasoning_format", s.llm_reasoning_format),
                                            ("reasoning_effort", s.llm_reasoning_effort)) if v}
        self._client = client
        self._api_key = s.groq_api_key

    @property
    def client(self) -> AsyncGroq:
        if self._client is None:
            if not self._api_key:
                raise LLMUnavailable("GROQ_API_KEY is not set.")
            # One retry: Groq's retry-after on a per-minute limit can be 20s+, too long for a shop counter.
            self._client = AsyncGroq(api_key=self._api_key, timeout=20.0, max_retries=1)
        return self._client

    async def chat(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
        request = dict(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            tools=openai_tools(tools),
            tool_choice="auto",
            temperature=self.temperature,
            max_completion_tokens=self.max_tokens,
            **self.reasoning,
        )
        for attempt in range(2):
            try:
                resp = await self.client.chat.completions.create(**request)
                break
            except groq.BadRequestError as exc:
                # Groq returns 400 "tool_use_failed" when the model emits a malformed tool call; retry once.
                if "tool_use_failed" in str(exc) and attempt == 0:
                    log.warning("tool_use_failed, retrying")
                    continue
                raise LLMUnavailable(str(exc)) from exc
            except groq.RateLimitError as exc:  # after the SDK's own retry
                raise LLMUnavailable(str(exc), rate_limited=True) from exc
            except groq.APIError as exc:  # connection, timeout, 5xx (after SDK retries)
                raise LLMUnavailable(str(exc)) from exc

        msg = resp.choices[0].message
        calls = [
            ToolCall(tc.id, tc.function.name, _parse_args(tc.function.arguments), tc.function.arguments or "")
            for tc in (msg.tool_calls or [])
        ]
        content = _THINK.sub("", msg.content or "").strip()  # in case reasoning leaks into content
        return _turn(content, calls)


def _turn(content: str, calls: list[ToolCall]) -> LLMTurn:
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.raw_arguments or "{}"}}
            for c in calls
        ]
    return LLMTurn(text=content, tool_calls=calls, message=message)


class Gemma4LLM:
    """Gemma 4 behind an OpenAI-compatible /chat/completions endpoint (GEMMA4_BASE_URL)."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        s = get_settings()
        self.model = s.gemma4_model
        self.temperature = s.llm_temperature
        self.max_tokens = s.llm_max_tokens
        self._base_url = s.gemma4_base_url.rstrip("/")
        self._api_key = s.gemma4_api_key
        self._timeout = s.gemma4_timeout_s
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self._api_key:
                raise LLMUnavailable("GEMMA4_API_KEY is not set.")
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        return self._client

    async def chat(self, *, system: str, messages: list[dict], tools: list[dict]) -> LLMTurn:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = openai_tools(tools)
            body["tool_choice"] = "auto"

        data = None
        for attempt in range(2):  # one retry for transient failures; a shop counter can't wait longer
            try:
                resp = await self.client.post("/chat/completions", json=body)
            except httpx.HTTPError as exc:  # connection error / timeout
                if attempt == 0:
                    log.warning("gemma4 request failed (%s), retrying", exc)
                    continue
                raise LLMUnavailable(f"Gemma 4 unreachable: {exc}") from exc
            if resp.status_code == 429:
                raise LLMUnavailable(f"Gemma 4 rate limited: {resp.text[:200]}", rate_limited=True)
            if resp.status_code >= 500 and attempt == 0:
                log.warning("gemma4 %s, retrying", resp.status_code)
                continue
            if resp.status_code >= 400:
                raise LLMUnavailable(f"Gemma 4 error {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            break
        if not data or not data.get("choices"):
            raise LLMUnavailable(f"Gemma 4 returned no choices: {str(data)[:300]}")

        msg = data["choices"][0].get("message") or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments")
            if isinstance(raw, dict):  # some servers send parsed arguments
                raw = json.dumps(raw)
            calls.append(ToolCall(tc.get("id") or f"call_{i}", fn.get("name", ""), _parse_args(raw), raw or ""))
        content = _THINK.sub("", msg.get("content") or "").strip()
        return _turn(content, calls)


def make_llm() -> LLMClient:
    """The configured LLM (LLM_PROVIDER): gemma4 by default, groq as the alternative."""
    provider = get_settings().llm_provider.strip().lower()
    if provider == "groq":
        return GroqLLM()
    if provider in ("gemma4", "gemma"):
        return Gemma4LLM()
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r}: use 'gemma4' or 'groq'.")
