from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.merchants import merchant_out
from app.db.models import Merchant
from app.demo.seed import seed_demo

router = APIRouter(prefix="/demo", tags=["demo"])
_PAGE = Path(__file__).with_name("simulate.html")
_TESTER = Path(__file__).with_name("tester.html")


@router.post("/seed", status_code=201)
async def seed(session: AsyncSession = Depends(get_session)):
    m = await seed_demo(session)
    await session.commit()
    return merchant_out(m)


@router.get("/merchants")
async def merchants(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Merchant).order_by(Merchant.created_at.desc()))).scalars()
    return {"merchants": [merchant_out(m) for m in rows]}


@router.get("/simulate", response_class=HTMLResponse)
async def simulate_page():
    return _PAGE.read_text(encoding="utf-8")


@router.get("/tester", response_class=HTMLResponse)
async def tester_page():
    """Manual API tester: every endpoint as a form, with responses, audio playback and live events."""
    return _TESTER.read_text(encoding="utf-8")
