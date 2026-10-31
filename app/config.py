from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(Path(__file__).resolve().parent.parent / ".env", ".env"),
        extra="ignore"
    )

    database_url: str = "sqlite+aiosqlite:///./khata.db"

    # LLM (Groq)
    groq_api_key: str = ""
    llm_model: str = "qwen/qwen3.8-27b"
    # Qwen3 reasons before answering; "hidden" keeps that out of the reply. Empty = don't send.
    llm_reasoning_format: str = "hidden"
    llm_reasoning_effort: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1024
    llm_max_rounds: int = 6

    # Speech (Sarvam AI)
    sarvam_api_key: str = ""
    sarvam_base_url: str = "https://api.sarvam.ai"
    # saaras:v3 + translit gives Roman-script Hinglish ("saadhe teen sau"), which the
    # number/name normalizers are built for. saaras:v4 (no mode) returns native script.
    sarvam_stt_model: str = "saaras:v3"
    sarvam_stt_mode: str = "translit"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_tts_speaker: str = "priya"
    sarvam_tts_pace: float = 1.1
    voice_clip_dir: str = "./voice_clips"

    # Agent behaviour
    pending_action_ttl_seconds: int = 120
    history_max_messages: int = 40
    salary_day: int = 1  # "salary ke baad" resolves to this day of next month

    # Scheduler (IST times)
    enable_scheduler: bool = False
    reminder_hour: int = 9
    summary_hour: int = 21

    timezone: str = "Asia/Kolkata"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
