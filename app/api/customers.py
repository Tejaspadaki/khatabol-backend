"""Manual (non-voice) khata operations for the app's ledger and customer screens."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import customer_card, entry_card
from app.api.deps import get_session, today_ist
from app.api.schemas import CreditIn, CustomerIn, PaymentIn, UpiLinkIn
from app.db.models import EntrySource, PaymentMode
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.nlp.dates import resolve_due_date
from app.nlp.hinglish_numbers import format_rupees, rupees_to_paise

router = APIRouter(prefix="/api/merchants/{merchant_id}", tags=["khata"])


@router.get("/customers")
async def list_customers(
    merchant_id: uuid.UUID, q: str | None = Query(None, description="fuzzy name search"),
    session: AsyncSession = Depends(get_session),
):
    await ledger.get_merchant(session, merchant_id)
    if q:
        found = await ledger.find_customers(session, merchant_id, q)
        return {
            "decision": found["decision"],
            "customers": [{**customer_card(m["customer"]), "score": m["score"]} for m in found["matches"]],
        }
    return {"customers": [customer_card(c) for c in await ledger.list_customers(session, merchant_id)]}


@router.post("/customers", status_code=201)
async def create_customer(merchant_id: uuid.UUID, body: CustomerIn, session: AsyncSession = Depends(get_session)):
    await ledger.get_merchant(session, merchant_id)
    c = await ledger.create_customer(session, merchant_id, body.name, body.phone, body.nicknames)
    await session.commit()
    return customer_card(c)


@router.get("/customers/{customer_id}")
async def get_customer(merchant_id: uuid.UUID, customer_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    b = await ledger.get_balance(session, merchant_id, customer_id, today_ist())
    c, lp = b["customer"], b["last_payment"]
    return {
        **customer_card(c),
        "ref_code": c.ref_code,
        "upi_ids": await ledger.upi_ids(session, c.id),
        "next_due_date": b["next_due_date"],
        "next_due": format_rupees(b["next_due_paise"]) if b["next_due_date"] else None,
        "overdue": format_rupees(b["overdue_paise"]),
        "last_payment": entry_card(lp, c.name) if lp else None,
    }


@router.get("/customers/{customer_id}/history")
async def history(
    merchant_id: uuid.UUID, customer_id: uuid.UUID, limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    entries = await ledger.get_history(session, merchant_id, customer_id, limit)
    return {"entries": [entry_card(e) for e in entries]}


@router.post("/customers/{customer_id}/credit", status_code=201)
async def add_credit(
    merchant_id: uuid.UUID, customer_id: uuid.UUID, body: CreditIn, session: AsyncSession = Depends(get_session)
):
    due = None
    if body.due:
        due = resolve_due_date(body.due, today_ist())
        if due is None:
            raise LedgerError.of("err_bad_due", due=body.due)
    e = await ledger.add_credit(session, merchant_id, customer_id, rupees_to_paise(body.amount_rupees), due,
                                body.items, body.note, EntrySource.MANUAL)
    c = await ledger.get_customer(session, merchant_id, customer_id)
    await session.commit()
    return {"entry": entry_card(e, c.name), "customer": customer_card(c)}


@router.post("/customers/{customer_id}/payment", status_code=201)
async def record_payment(
    merchant_id: uuid.UUID, customer_id: uuid.UUID, body: PaymentIn, session: AsyncSession = Depends(get_session)
):
    e = await ledger.record_payment(session, merchant_id, customer_id, rupees_to_paise(body.amount_rupees),
                                    PaymentMode(body.mode.upper()), EntrySource.MANUAL, note=body.note)
    c = await ledger.get_customer(session, merchant_id, customer_id)
    await session.commit()
    return {"entry": entry_card(e, c.name), "customer": customer_card(c)}


@router.post("/customers/{customer_id}/upi", status_code=201)
async def link_upi(
    merchant_id: uuid.UUID, customer_id: uuid.UUID, body: UpiLinkIn, session: AsyncSession = Depends(get_session)
):
    h = await ledger.link_upi(session, merchant_id, customer_id, body.vpa, linked_via="manual", verified=True)
    await session.commit()
    return {"customer_id": str(customer_id), "vpa": h.vpa}


@router.post("/entries/{entry_id}/reverse", status_code=201)
async def reverse_entry(merchant_id: uuid.UUID, entry_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    e = await ledger.reverse_entry(session, merchant_id, entry_id, note="reversed from app", source=EntrySource.MANUAL)
    c = await ledger.get_customer(session, merchant_id, e.customer_id)
    await session.commit()
    return {"entry": entry_card(e, c.name), "customer": customer_card(c)}
