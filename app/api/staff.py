"""Staff, owner approvals for staff edits, and edit history (plan.md §19)."""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import pending as gate
from app.api.deps import get_session
from app.api.schemas import StaffIn
from app.db.models import PendingAction, PendingStatus
from app.i18n import t
from app.ledger import edits
from app.ledger import service as ledger
from app.notify import service as notify

router = APIRouter(prefix="/api/merchants/{merchant_id}", tags=["staff"])


async def _owner(session: AsyncSession, merchant_id: uuid.UUID, staff_id: str | None) -> str:
    actor = await edits.actor_for(session, merchant_id, uuid.UUID(staff_id) if staff_id else None)
    if actor != "owner":
        raise HTTPException(403, t((await ledger.get_merchant(session, merchant_id)).language, "err_owner_only"))
    return actor


@router.post("/staff", status_code=201)
async def add_staff(merchant_id: uuid.UUID, body: StaffIn, x_staff_id: str | None = Header(None),
                    session: AsyncSession = Depends(get_session)):
    await ledger.get_merchant(session, merchant_id)
    await _owner(session, merchant_id, x_staff_id)
    s = await edits.add_staff(session, merchant_id, body.name, body.role)
    await session.commit()
    return {"staff_id": str(s.id), "name": s.name, "role": s.role}


@router.get("/staff")
async def list_staff(merchant_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return {"staff": [{"staff_id": str(s.id), "name": s.name, "role": s.role}
                      for s in await edits.list_staff(session, merchant_id)]}


@router.get("/approvals")
async def approvals(merchant_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """Staff edits confirmed by staff and waiting for the owner."""
    stmt = (select(PendingAction).where(PendingAction.merchant_id == merchant_id,
                                        PendingAction.status == PendingStatus.AWAITING_APPROVAL)
            .order_by(PendingAction.created_at))
    names = await edits.actor_names(session, merchant_id)
    return {"approvals": [{"action_id": str(p.id), "tool": p.tool, "summary": p.summary,
                           "requested_by": names.get(p.args.get("edited_by"), p.args.get("edited_by")),
                           "at": p.created_at.isoformat()} for p in (await session.execute(stmt)).scalars()]}


@router.post("/approvals/{action_id}/approve")
async def approve(merchant_id: uuid.UUID, action_id: uuid.UUID, x_staff_id: str | None = Header(None),
                  session: AsyncSession = Depends(get_session)):
    actor = await _owner(session, merchant_id, x_staff_id)
    out = await gate.approve(session, await ledger.get_merchant(session, merchant_id), action_id, actor)
    await notify.commit(session)
    return out


@router.post("/approvals/{action_id}/reject")
async def reject(merchant_id: uuid.UUID, action_id: uuid.UUID, x_staff_id: str | None = Header(None),
                 session: AsyncSession = Depends(get_session)):
    actor = await _owner(session, merchant_id, x_staff_id)
    out = await gate.reject(session, await ledger.get_merchant(session, merchant_id), action_id, actor)
    await session.commit()
    return out


@router.get("/entries/{entry_id}/edits")
async def entry_edits(merchant_id: uuid.UUID, entry_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """Edit history of an entry: old value -> new value, who and when."""
    names = await edits.actor_names(session, merchant_id)
    return {"edits": [edits.edit_card(r, names) for r in await edits.entry_history(session, merchant_id, entry_id)]}


@router.get("/customers/{customer_id}/edits")
async def customer_edits(merchant_id: uuid.UUID, customer_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    await ledger.get_customer(session, merchant_id, customer_id)
    names = await edits.actor_names(session, merchant_id)
    return {"edits": [edits.edit_card(r, names) for r in await edits.customer_edits(session, merchant_id, customer_id)]}
