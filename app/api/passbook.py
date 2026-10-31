"""Customer passbook (plan.md §18.3): no app install, OTP login, the person's own khatas only."""

from pathlib import Path

from fastapi import APIRouter, Depends, Header
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, today_ist
from app.api.schemas import InboundMessageIn, OtpIn, PassbookDisputeIn, PassbookLoginIn, PassbookPrefIn
from app.db.models import Customer
from app.notify import passbook
from app.notify.service import account_for_phone

router = APIRouter(tags=["passbook"])
_PAGE = Path(__file__).resolve().parent.parent / "web" / "passbook.html"


def _token(authorization: str | None) -> str | None:
    return authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None


async def _account(authorization: str | None = Header(None), session: AsyncSession = Depends(get_session)):
    return await passbook.account_for_token(session, _token(authorization))


@router.get("/passbook", include_in_schema=False)
async def page():
    return FileResponse(_PAGE, media_type="text/html")


@router.post("/api/passbook/otp")
async def otp(body: OtpIn, session: AsyncSession = Depends(get_session)):
    """Send a login code by SMS. The answer is the same whether or not the number has a khata."""
    await passbook.request_otp(session, body.phone)
    return {"sent": True}


@router.post("/api/passbook/login")
async def login(body: PassbookLoginIn, session: AsyncSession = Depends(get_session)):
    return {"token": await passbook.verify_otp(session, body.phone, body.code)}


@router.get("/api/passbook")
async def my_passbook(acc=Depends(_account), session: AsyncSession = Depends(get_session)):
    return await passbook.view(session, acc, today_ist())


@router.post("/api/passbook/disputes", status_code=201)
async def dispute(body: PassbookDisputeIn, acc=Depends(_account), session: AsyncSession = Depends(get_session)):
    """"Galat hai" on one of my entries."""
    d = await passbook.raise_dispute(session, acc, body.entry_id, body.reason)
    return {"dispute_id": str(d.id), "status": d.status}


@router.post("/api/passbook/prefs")
async def prefs(body: PassbookPrefIn, acc=Depends(_account), session: AsyncSession = Depends(get_session)):
    """instant (default), digest (one daily summary), or off."""
    await passbook.set_pref(session, acc, body.merchant_id, body.mode)
    return {"merchant_id": str(body.merchant_id), "mode": body.mode}


@router.post("/api/notify/inbound")
async def inbound(body: InboundMessageIn, session: AsyncSession = Depends(get_session)):
    """Replies from customers (WhatsApp/SMS webhook in production). "STOP" opts out of every shop."""
    acc = await account_for_phone(session, body.phone, create=False)
    stopped = 0
    if acc and body.text.strip().upper() in {"STOP", "BAND", "UNSUBSCRIBE"}:
        for c in (await session.execute(select(Customer).where(Customer.customer_account_id == acc.id))).scalars():
            c.notify_pref, stopped = "off", stopped + 1
        await session.commit()
    return {"opted_out": stopped}

