"""UPI Udhaar Check for the app (plan.md §22): advice only, it never writes anything."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, today_ist
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.nlp.hinglish_numbers import rupees_to_paise
from app.risk import trust_score

router = APIRouter(prefix="/api/merchants/{merchant_id}", tags=["risk"])


@router.get("/risk")
async def udhaar_risk(
    merchant_id: uuid.UUID,
    customer_id: uuid.UUID | None = None,
    vpa: str | None = Query(None, max_length=100),
    name: str | None = Query(None, max_length=200, description="Name the person gave, for the UPI name check"),
    amount_rupees: float | None = Query(None, gt=0),
    session: AsyncSession = Depends(get_session),
):
    """TrustScore level, suggested amount and reasons for a customer or a UPI ID."""
    merchant = await ledger.get_merchant(session, merchant_id)
    if customer_id is None and not vpa:
        raise LedgerError("Give customer_id or vpa.")
    customer = await ledger.get_customer(session, merchant_id, customer_id) if customer_id else None
    a = await trust_score.assess(session, merchant, today_ist(), customer=customer, vpa=vpa, given_name=name)
    return trust_score.risk_card(a, rupees_to_paise(amount_rupees) if amount_rupees else None)
