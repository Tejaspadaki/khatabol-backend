"""Incoming UPI payments -> khata auto-settlement.

There is no live payment-platform integration in this prototype: payments arrive through
POST .../payments/incoming (the demo's "Customer pays" button). The reconciler behind it is
the part a payment platform would call on every successful UPI credit.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import i18n
from app.agent.tools import customer_card
from app.api.deps import get_session
from app.api.schemas import AssignIn, IncomingPaymentIn
from app.ledger import service as ledger
from app.nlp.hinglish_numbers import format_rupees, rupees_to_paise
from app.payments import reconciler
from app.speech import balance_phrase

router = APIRouter(prefix="/api/merchants/{merchant_id}/payments", tags=["payments"])


def _txn_out(t) -> dict:
    return {
        "txn_id": t.txn_id,
        "status": t.status.value,
        "payer_vpa": t.payer_vpa,
        "payer_name": t.payer_name,
        "amount": format_rupees(t.amount_paise),
        "amount_paise": t.amount_paise,
        "customer_id": str(t.customer_id) if t.customer_id else None,
        "paid_at": t.paid_at,
    }


@router.post("/incoming")
async def incoming_payment(merchant_id: uuid.UUID, body: IncomingPaymentIn,
                           session: AsyncSession = Depends(get_session)):
    """Record a received UPI payment and auto-settle udhaar. Repeating a txn_id is a no-op."""
    await ledger.get_merchant(session, merchant_id)
    event = reconciler.PaymentEvent(
        merchant_id=merchant_id,
        txn_id=body.txn_id or f"SIM{uuid.uuid4().hex[:12].upper()}",
        payer_vpa=body.payer_vpa,
        amount_paise=rupees_to_paise(body.amount_rupees),
        payer_name=body.payer_name,
        note=body.note,
    )
    result = await reconciler.handle_payment(session, event)
    await session.commit()
    result.publish()
    return {"duplicate": result.duplicate, "announcement": result.announcement, **_txn_out(result.txn)}


@router.get("/unmatched")
async def unmatched(merchant_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return {"payments": [_txn_out(t) for t in await reconciler.list_unmatched(session, merchant_id)]}


@router.post("/{txn_id}/assign")
async def assign(merchant_id: uuid.UUID, txn_id: str, body: AssignIn, session: AsyncSession = Depends(get_session)):
    txn, c = await reconciler.assign_payment(session, merchant_id, txn_id, body.customer_id)
    await session.commit()
    lang = (await ledger.get_merchant(session, merchant_id)).language
    return {
        **_txn_out(txn),
        "customer": customer_card(c),
        "spoken": i18n.t(lang, "upi_assigned", amount=format_rupees(txn.amount_paise), name=c.name,
                    balance=balance_phrase(c.balance_paise, lang)) + ".",
    }


@router.post("/{txn_id}/undo")
async def undo(merchant_id: uuid.UUID, txn_id: str, session: AsyncSession = Depends(get_session)):
    """'Ye Ramesh ka nahi tha': reverse the auto-settlement and ask again."""
    txn = await reconciler.undo_auto_payment(session, merchant_id, txn_id)
    await session.commit()
    return _txn_out(txn)
