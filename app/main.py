import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    agent, collections, customers, families, merchants, notifications, passbook, payments, risk, staff, ws,
)
from app.config import get_settings
from app.i18n import lang_of
from app.db.base import get_engine
from app.db.migrate import upgrade_to_head
from app.demo import routes as demo
from app.ledger.errors import LedgerError, NotFoundError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring the schema to the latest Alembic revision (stamps pre-migration databases at the baseline first).
    await upgrade_to_head(get_engine())
    scheduler = None
    if get_settings().enable_scheduler:
        from app.scheduler.jobs import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Khata Voice", version="0.1.0", lifespan=lifespan)

# The Expo app (Khatabol-frontend) runs on its own origin when served for web, e.g. localhost:8081.
# Hackathon: allow any origin. Restrict to the app's real origins before production.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

for r in (merchants.router, customers.router, agent.router, collections.router,
          payments.router, risk.router, staff.router, notifications.router, passbook.router, families.router, ws.router,
          demo.router):
    app.include_router(r)

# Voice clips are the proof attached to each khata entry (voice_logs.audio_url).
_clips = Path(get_settings().voice_clip_dir)
_clips.mkdir(parents=True, exist_ok=True)
app.mount("/voice-clips", StaticFiles(directory=_clips), name="voice-clips")


@app.exception_handler(LedgerError)
async def ledger_error(request: Request, exc: LedgerError):
    """The app sends the shop's language as Accept-Language; errors come back in it."""
    from app.notify.passbook import PassbookAuthError

    detail = exc.text(lang_of(request.headers.get("accept-language")))
    code = 404 if isinstance(exc, NotFoundError) else 401 if isinstance(exc, PassbookAuthError) else 400
    return JSONResponse({"detail": detail}, status_code=code)


_WEB_APP = Path(__file__).parent / "web" / "index.html"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/app")


@app.get("/app", include_in_schema=False)
async def web_app():
    """Merchant app: real voice (Sarvam STT/TTS), agent, khata, dues, simulated UPI payments, live announcements."""
    return FileResponse(_WEB_APP, media_type="text/html")


@app.get("/health")
async def health():
    return {"ok": True}
