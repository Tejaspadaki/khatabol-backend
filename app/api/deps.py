from datetime import date, datetime

from fastapi import Request

from app.agent.llm import make_llm
from app.agent.orchestrator import Orchestrator
from app.config import get_settings
from app.db.base import get_session  # noqa: F401  (re-exported for routers)
from app.voice.sarvam import SarvamClient


def today_ist() -> date:
    return datetime.now(get_settings().tz).date()


def get_orchestrator(request: Request) -> Orchestrator:
    """Created on first use so the API runs (ledger, payments) without an LLM key."""
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        orch = Orchestrator(make_llm())
        request.app.state.orchestrator = orch
    return orch


def get_sarvam(request: Request) -> SarvamClient:
    client = getattr(request.app.state, "sarvam", None)
    if client is None:
        client = SarvamClient()
        request.app.state.sarvam = client
    return client
