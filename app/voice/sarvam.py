"""Sarvam AI speech-to-text (saaras) and text-to-speech (bulbul) over REST."""

import base64
import logging
import re
from dataclasses import dataclass

import httpx

from app.config import get_settings

log = logging.getLogger("khata.sarvam")

LANGUAGE_CODES = {"hi": "hi-IN", "mr": "mr-IN", "en": "en-IN"}
_RUPEES = re.compile(r"-?₹\s?([\d,]+(?:\.\d+)?)")


class SpeechUnavailable(Exception):
    """Sarvam could not be reached or rejected the request."""


@dataclass
class Transcript:
    text: str
    language_code: str | None
    request_id: str | None = None


def for_speech(text: str, language: str = "hi") -> str:
    """'₹1,500 baaki' -> '1,500 rupaye baaki' so TTS reads money naturally."""
    word = "rupees" if language == "en" else "rupaye"
    return _RUPEES.sub(lambda m: f"{m.group(1)} {word}", text)


class SarvamClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        s = get_settings()
        self.s = s
        self._http = http or httpx.AsyncClient(base_url=s.sarvam_base_url, timeout=20.0)

    def _headers(self) -> dict:
        if not self.s.sarvam_api_key:
            raise SpeechUnavailable("SARVAM_API_KEY is not set.")
        return {"api-subscription-key": self.s.sarvam_api_key}

    async def _post(self, path: str, **kwargs) -> dict:
        try:
            r = await self._http.post(path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise SpeechUnavailable(f"Sarvam {path} unreachable: {exc}") from exc
        if r.status_code != 200:
            raise SpeechUnavailable(f"Sarvam {path} returned {r.status_code}: {r.text[:300]}")
        return r.json()

    async def transcribe(self, audio: bytes, filename: str, content_type: str, language: str = "hi") -> Transcript:
        data = {"model": self.s.sarvam_stt_model, "language_code": LANGUAGE_CODES.get(language, "unknown")}
        if self.s.sarvam_stt_mode and self.s.sarvam_stt_model.startswith("saaras:v3"):
            data["mode"] = self.s.sarvam_stt_mode
        body = await self._post("/speech-to-text", data=data, files={"file": (filename, audio, content_type)})
        return Transcript((body.get("transcript") or "").strip(), body.get("language_code"), body.get("request_id"))

    async def synthesize(self, text: str, language: str = "hi") -> bytes:
        """Returns WAV bytes."""
        payload = {
            "text": for_speech(text, language)[:2500],
            "language_code": LANGUAGE_CODES.get(language, "hi-IN"),
            "model": self.s.sarvam_tts_model,
            "speaker": self.s.sarvam_tts_speaker,
            "pace": self.s.sarvam_tts_pace,
        }
        body = await self._post("/text-to-speech", json=payload)
        audios = body.get("audios") or []
        if not audios:
            raise SpeechUnavailable("Sarvam TTS returned no audio.")
        return base64.b64decode(audios[0])
