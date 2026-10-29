"""LLM behind one small interface so the orchestrator is testable and the vendor is swappable.

Messages use the OpenAI-style chat format (user / assistant with tool_calls / tool), which Groq speaks.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import groq
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
        message: dict = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.raw_arguments or "{}"}}
                for c in calls
            ]
        return LLMTurn(text=content, tool_calls=calls, message=message)
