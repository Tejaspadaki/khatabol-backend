import base64
import logging
import mimetypes
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.orchestrator import Orchestrator, TurnResult
from app.api.deps import get_orchestrator, get_sarvam, get_session
from app.api.schemas import AgentMessageIn, TtsIn
from app.config import get_settings
from app.ledger import service as ledger
from app.voice.sarvam import SarvamClient, SpeechUnavailable

log = logging.getLogger("khata.api.agent")
router = APIRouter(prefix="/api/merchants/{merchant_id}", tags=["agent"])

MAX_AUDIO_BYTES = 10 * 1024 * 1024  # push-to-talk clips are a few seconds


async def _speak(sarvam: SarvamClient, text: str, language: str) -> dict:
    try:
        wav = await sarvam.synthesize(text, language)
    except SpeechUnavailable as exc:
        log.warning("TTS failed: %s", exc)
        return {"audio_base64": None, "tts_error": str(exc)}
    return {"audio_base64": base64.b64encode(wav).decode(), "audio_mime": "audio/wav"}


def _out(result: TurnResult) -> dict:
    return {**asdict(result), "voice_log_id": str(result.voice_log_id)}


@router.post("/agent/message")
async def agent_message(
    merchant_id: uuid.UUID,
    body: AgentMessageIn,
    session: AsyncSession = Depends(get_session),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    sarvam: SarvamClient = Depends(get_sarvam),
):
    """Text-mode agent turn. Set speak=true to also get the reply as Sarvam TTS audio."""
    result = await orchestrator.handle_text(session, merchant_id, body.text, body.session_id)
    out = _out(result)
    if body.speak:
        merchant = await ledger.get_merchant(session, merchant_id)
        out |= await _speak(sarvam, result.reply, merchant.language)
    return out


@router.post("/agent/voice")
async def agent_voice(
    merchant_id: uuid.UUID,
    audio: UploadFile = File(..., description="Push-to-talk clip: webm, wav, mp3, ogg..."),
    session_id: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
    orchestrator: Orchestrator = Depends(get_orchestrator),
    sarvam: SarvamClient = Depends(get_sarvam),
):
    """Voice turn: Sarvam STT -> agent -> Sarvam TTS. The clip is kept as proof for the khata entry."""
    merchant = await ledger.get_merchant(session, merchant_id)
    data = await audio.read()
    if not data:
        raise HTTPException(400, "empty audio")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio clip too long; keep push-to-talk under 30 seconds")

    content_type = (audio.content_type or "audio/webm").split(";")[0]
    ext = Path(audio.filename or "").suffix or mimetypes.guess_extension(content_type) or ".webm"
    clip_dir = Path(get_settings().voice_clip_dir)
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_name = f"{uuid.uuid4().hex}{ext}"
    (clip_dir / clip_name).write_bytes(data)

    try:
        transcript = await sarvam.transcribe(data, f"clip{ext}", content_type, merchant.language)
    except SpeechUnavailable as exc:
        log.error("STT failed: %s", exc)
        raise HTTPException(503, f"Speech-to-text unavailable: {exc}") from exc

    result = await orchestrator.handle_text(
        session, merchant_id, transcript.text, session_id, audio_url=f"/voice-clips/{clip_name}",
    )
    return {"transcript": transcript.text, "stt_language": transcript.language_code, **_out(result),
            **await _speak(sarvam, result.reply, merchant.language)}


@router.post("/tts", response_class=Response)
async def tts(merchant_id: uuid.UUID, body: TtsIn, session: AsyncSession = Depends(get_session),
              sarvam: SarvamClient = Depends(get_sarvam)):
    """Speak any text (announcements, summaries) in the merchant's language. Returns audio/wav."""
    merchant = await ledger.get_merchant(session, merchant_id)
    try:
        wav = await sarvam.synthesize(body.text, merchant.language)
    except SpeechUnavailable as exc:
        raise HTTPException(503, f"Text-to-speech unavailable: {exc}") from exc
    return Response(wav, media_type="audio/wav")
