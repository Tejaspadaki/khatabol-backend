import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import customer_card
from app.api.deps import get_session, today_ist
from app.api.schemas import ReminderIn
from app.ledger import service as ledger
from app.nlp.hinglish_numbers import format_rupees
from app.scheduler.jobs import summary_text
from app.scheduler.reminders import send_reminders

router = APIRouter(prefix="/api/merchants/{merchant_id}", tags=["collections"])


@router.get("/dues")
async def dues(
    merchant_id: uuid.UUID,
    filter: str = Query("all", pattern="^(today|overdue|this_week|all)$"),
    session: AsyncSession = Depends(get_session),
):
    rows = await ledger.list_dues(session, merchant_id, filter, today_ist())
    return {
        "filter": filter,
        "total_due": format_rupees(sum(r["due_paise"] for r in rows)),
        "customers": [
            {
                **customer_card(r["customer"]),
                "due": format_rupees(r["due_paise"]),
                "due_paise": r["due_paise"],
                "overdue": format_rupees(r["overdue_paise"]),
                "next_due_date": r["next_due_date"],
            }
            for r in rows
        ],
    }


@router.post("/reminders", status_code=201)
async def reminders(merchant_id: uuid.UUID, body: ReminderIn, session: AsyncSession = Depends(get_session)):
    merchant = await ledger.get_merchant(session, merchant_id)
    today = today_ist()
    if body.all_due:
        rows = await ledger.list_dues(session, merchant_id, "today", today)
        targets = [(r["customer"], r["balance_paise"], r["next_due_date"]) for r in rows]
    else:
        targets = []
        for cid in body.customer_ids:
            b = await ledger.get_balance(session, merchant_id, cid, today)
            targets.append((b["customer"], b["balance_paise"], b["next_due_date"]))
    sent = await send_reminders(session, merchant, targets)
    await session.commit()
    return {
        "sent": len(sent),
        "reminders": [
            {"customer_id": str(r.customer_id), "amount": format_rupees(r.amount_paise), "status": r.status,
             "payment_link": r.payment_link, "message": r.message}
            for r in sent
        ],
    }


@router.get("/summary")
async def summary(merchant_id: uuid.UUID, day: date | None = None, session: AsyncSession = Depends(get_session)):
    s = await ledger.daily_summary(session, merchant_id, day or today_ist())
    return {
        **s,
        "credit_given": format_rupees(s["credit_given_paise"]),
        "collected": format_rupees(s["collected_paise"]),
        "outstanding": format_rupees(s["outstanding_paise"]),
        "spoken": summary_text(s),
    }
