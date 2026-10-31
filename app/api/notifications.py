"""Customer notifications and disputes, merchant side (plan.md §18)."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas import DisputeConfirmIn
from app.db.models import Customer, LedgerEntry, Notification
from app.ledger import service as ledger
from app.nlp.hinglish_numbers import format_rupees
from app.notify import disputes
from app.notify import service as notify

router = APIRouter(prefix="/api/merchants/{merchant_id}", tags=["notifications"])


@router.get("/notifications")
async def notifications(merchant_id: uuid.UUID, customer_id: uuid.UUID | None = None,
                        limit: int = Query(50, ge=1, le=500), session: AsyncSession = Depends(get_session)):
    """Messages to customers, newest first, with their delivery status (the demo's "customer's phone")."""
    await ledger.get_merchant(session, merchant_id)
    stmt = select(Notification).where(Notification.merchant_id == merchant_id)
    if customer_id:
        stmt = stmt.where(Notification.customer_id == customer_id)
    rows = (await session.execute(stmt.order_by(Notification.created_at.desc()).limit(limit))).scalars()
    return {"notifications": [await notify.notification_card(session, n) for n in rows]}


async def _dispute_card(session, d) -> dict:
    c = await session.get(Customer, d.customer_id)
    e = await session.get(LedgerEntry, d.ledger_entry_id)
    return {"dispute_id": str(d.id), "customer_id": str(c.id), "customer": c.name, "entry_id": str(e.id),
            "amount": format_rupees(e.amount_paise), "type": e.type.value, "raised_by": d.raised_by,
            "reason": d.reason, "status": d.status, "at": d.created_at.isoformat(),
            "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None}


@router.get("/disputes")
async def list_disputes(merchant_id: uuid.UUID, status: str | None = Query("OPEN"),
                        session: AsyncSession = Depends(get_session)):
    await ledger.get_merchant(session, merchant_id)
    return {"disputes": [await _dispute_card(session, d)
                         for d in await disputes.list_disputes(session, merchant_id, status or None)]}


@router.post("/disputes/{dispute_id}/confirm")
async def confirm_dispute(merchant_id: uuid.UUID, dispute_id: uuid.UUID, body: DisputeConfirmIn,
                          session: AsyncSession = Depends(get_session)):
    """The entry is right: close the dispute and tell the customer. To fix it instead, edit the entry (§19)."""
    d = await disputes.confirm_dispute(session, merchant_id, dispute_id, body.note)
    await notify.commit(session)
    return await _dispute_card(session, d)
