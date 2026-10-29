"""One agent turn: text in -> (confirmation gate | LLM tool loop) -> short spoken reply.

The voice endpoint calls handle_text() with the Sarvam STT transcript.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import pending as gate
from app.agent.llm import LLMClient, LLMUnavailable
from app.agent.prompts import system_prompt, user_turn
from app.agent.tools import TOOL_SCHEMAS, WRITE_TOOLS, ToolContext, active_pending, execute_tool, to_json
from app.config import get_settings
from app.db.models import VoiceLog
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.nlp.confirm import classify_confirmation, classify_goodbye
from app.nlp.hinglish_numbers import normalize_amounts

log = logging.getLogger("khata.agent")

LOW_CONFIDENCE = 0.6
FALLBACK_REPLY = "Maaf kijiye, samajh nahi aaya. Phir se boliye?"
LLM_DOWN_REPLY = "Abhi assistant se baat nahi ho pa rahi. Thodi der mein try kijiye, ya app se entry kijiye."
LLM_BUSY_REPLY = "Abhi bahut zyada requests aa rahi hain. Ek minute baad boliye, ya app se entry kijiye."
_AMOUNT_IN_SUMMARY = re.compile(r"₹[\d,]+(?:\.\d+)?")


@dataclass
class TurnResult:
    reply: str
    session_id: str
    voice_log_id: uuid.UUID
    outcome: str
    pending: list[dict] = field(default_factory=list)
    committed: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    latency_ms: int = 0


class HistoryStore:
    """Per-conversation message history, kept in memory.

    A conversation is replaced by a fresh one each day or once it gets long, rather than trimmed,
    so a tool call is never separated from its result.
    """

    def __init__(self) -> None:
        self._data: dict[tuple[uuid.UUID, str], tuple[date, list[dict]]] = {}

    def get(self, merchant_id: uuid.UUID, session_id: str, today: date) -> list[dict]:
        key = (merchant_id, session_id)
        day, msgs = self._data.get(key, (today, []))
        if day != today or len(msgs) >= get_settings().history_max_messages:
            msgs = []
        self._data[key] = (today, msgs)
        return msgs

    def reset(self, merchant_id: uuid.UUID, session_id: str) -> None:
        self._data.pop((merchant_id, session_id), None)


# create_customer is excluded: the model may still need its placeholder id for a follow-up call.
_SKIP_FINAL_TOOLS = WRITE_TOOLS - {"create_customer"} | {"cancel_pending"}


def _readback(proposed) -> str:
    return ". ".join(p.summary[0].upper() + p.summary[1:] for p in proposed) + ". Save karun?"


def _readback_ok(reply: str, summaries: list[str]) -> bool:
    """The spoken reply must contain every amount being saved, so the merchant confirms real numbers."""
    for s in summaries:
        for amt in _AMOUNT_IN_SUMMARY.findall(s):
            if amt not in reply:
                return False
    return bool(reply)


class Orchestrator:
    def __init__(self, llm: LLMClient, history: HistoryStore | None = None) -> None:
        self.llm = llm
        self.history = history or HistoryStore()

    async def handle_text(
        self,
        session: AsyncSession,
        merchant_id: uuid.UUID,
        text: str,
        session_id: str | None = None,
        stt_confidence: float | None = None,
        audio_url: str | None = None,
        now: datetime | None = None,
    ) -> TurnResult:
        started = time.monotonic()
        settings = get_settings()
        session_id = session_id or "default"
        merchant = await ledger.get_merchant(session, merchant_id)
        today = (now or datetime.now(settings.tz)).astimezone(settings.tz).date()
        text = " ".join((text or "").split())

        vlog = VoiceLog(merchant_id=merchant.id, session_id=session_id, transcript=text,
                        stt_confidence=stt_confidence, audio_url=audio_url)
        session.add(vlog)
        await session.flush()
        ctx = ToolContext(session, merchant, session_id, today, vlog.id)
        result = TurnResult(reply="", session_id=session_id, voice_log_id=vlog.id, outcome="")

        if not text or (stt_confidence is not None and stt_confidence < LOW_CONFIDENCE):
            result.reply, result.outcome = "Phir se boliye?", "low_confidence"
            return await self._finish(session, vlog, result, started)

        pending = await active_pending(session, merchant.id, session_id)
        history = self.history.get(merchant.id, session_id, today)

        # 0. Goodbye ("bas", "band karo", "dhanyavaad"): ends a hands-free conversation, no LLM.
        #    Pending entries are never dropped silently: "haan bas" saves them, plain "bas" says they weren't saved.
        goodbye = classify_goodbye(text)
        if goodbye:
            reply = ""
            if pending and goodbye == "save":
                out = await gate.confirm(ctx)
                result.committed = out.committed
                if out.outcome == "awaiting_second_confirm":  # big amount: one more "haan" before ending
                    result.reply, result.outcome = out.reply, out.outcome
                    result.pending = [self._card(p) for p in pending]
                    return await self._finish(session, vlog, result, started)
                reply = out.reply + " "
            elif pending:
                n = await gate.cancel(session, merchant.id, session_id)
                reply = f"{n} entry save nahi ki. " if n == 1 else f"{n} entries save nahi ki. "
            result.reply, result.outcome = reply + "Theek hai, phir milte hain.", "ended"
            self.history.reset(merchant.id, session_id)
            return await self._finish(session, vlog, result, started)

        # 1. Confirmation gate: plain yes/no never reaches the LLM.
        verdict = classify_confirmation(text) if pending else None
        if verdict == "yes":
            out = await gate.confirm(ctx)
            result.reply, result.outcome, result.committed = out.reply, out.outcome, out.committed
            if out.outcome == "awaiting_second_confirm":
                result.pending = [self._card(p) for p in pending]
        elif verdict == "no":
            await gate.cancel(session, merchant.id, session_id)
            result.reply, result.outcome = "Theek hai, cancel kar diya.", "cancelled"
        if verdict:
            if history:
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": result.reply})
            return await self._finish(session, vlog, result, started)

        # 2. LLM tool loop.
        normalized, _ = normalize_amounts(text)
        vlog.normalized_text = normalized
        history.append({"role": "user", "content": user_turn(text, normalized, pending)})
        system = system_prompt(merchant, today)
        reply = ""
        finished = False
        llm_down: str | None = None
        for _ in range(settings.llm_max_rounds):
            # Never hold a DB transaction open while waiting on the network (SQLite would lock everyone out).
            await session.commit()
            try:
                turn = await self.llm.chat(system=system, messages=history, tools=TOOL_SCHEMAS)
            except LLMUnavailable as exc:
                log.error("LLM unavailable: %s", exc)
                llm_down = LLM_BUSY_REPLY if exc.rate_limited else LLM_DOWN_REPLY
                break
            history.append(turn.message)
            if not turn.tool_calls:
                reply = turn.text
                finished = True
                break
            for tc in turn.tool_calls:
                call = {"name": tc.name, "input": tc.arguments if tc.arguments is not None else tc.raw_arguments}
                try:
                    if tc.arguments is None:
                        raise LedgerError("Arguments were not valid JSON; call the tool again.")
                    content = to_json(await execute_tool(ctx, tc.name, tc.arguments))
                    call["ok"] = True
                except LedgerError as exc:
                    content = f"Error: {exc}"
                    call["ok"], call["error"] = False, str(exc)
                history.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content})
                result.tool_calls.append(call)
            round_calls = result.tool_calls[-len(turn.tool_calls):]
            if ctx.proposed and all(c["ok"] and c["name"] in _SKIP_FINAL_TOOLS for c in round_calls):
                # Only proposals this round: the read-back is built from the summaries anyway, so skip
                # the extra LLM call (saves ~1s and a third of the tokens per write).
                reply = _readback(ctx.proposed)
                history.append({"role": "assistant", "content": reply})
                finished = True
                break
        if not finished:
            # Ran out of rounds or the call failed: start the next turn on a clean conversation.
            self.history.reset(merchant.id, session_id)

        proposed = ctx.proposed
        summaries = [p.summary for p in proposed]
        if proposed and not _readback_ok(reply, summaries):
            reply = _readback(proposed)
        result.reply = reply or llm_down or FALLBACK_REPLY
        result.pending = [self._card(p) for p in proposed]
        if proposed:
            result.outcome = "awaiting_confirmation"
        elif any(c["name"] in WRITE_TOOLS for c in result.tool_calls):
            result.outcome = "write_failed"
        else:
            result.outcome = "answered" if finished else ("llm_unavailable" if llm_down else "error")
        return await self._finish(session, vlog, result, started)

    @staticmethod
    def _card(p) -> dict:
        return {"action_id": str(p.id), "tool": p.tool, "summary": p.summary,
                "needs_double_confirmation": p.confirmations_needed > 1}

    @staticmethod
    async def _finish(session: AsyncSession, vlog: VoiceLog, result: TurnResult, started: float) -> TurnResult:
        result.latency_ms = int((time.monotonic() - started) * 1000)
        vlog.reply = result.reply
        vlog.outcome = result.outcome
        vlog.llm_tool_calls = result.tool_calls
        vlog.latency_ms = result.latency_ms
        await session.commit()
        return result
