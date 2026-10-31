"""Family khata views (plan.md §17). Writes go through the agent's gate (/agent/propose + "haan")."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.family import service as family
from app.ledger import service as ledger

router = APIRouter(prefix="/api/merchants/{merchant_id}", tags=["families"])


@router.get("/families")
async def families(merchant_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    await ledger.get_merchant(session, merchant_id)
    out = []
    for f in await family.list_families(session, merchant_id):
        b = await family.family_balance(session, merchant_id, f.id)
        out.append({"family_id": b["family_id"], "name": b["name"], "head": b["head"], "area_hint": b["area_hint"],
                    "total": b["total"], "total_paise": b["total_paise"], "members": len(b["members"])})
    return {"families": out}


@router.get("/families/{family_id}")
async def family_detail(merchant_id: uuid.UUID, family_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """Total plus per-member breakdown ("kisne kitna liya")."""
    return await family.family_balance(session, merchant_id, family_id)


@router.get("/families/{family_id}/qr")
async def family_qr(merchant_id: uuid.UUID, family_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """Content of the dynamic QR shown on screen at the counter: never printed, never stored."""
    return await family.family_pay_link(session, await ledger.get_merchant(session, merchant_id), family_id)
