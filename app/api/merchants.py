import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas import MerchantIn, MerchantUpdate
from app.db.models import Merchant
from app.ledger import service as ledger
from app.nlp.hinglish_numbers import format_rupees, rupees_to_paise

router = APIRouter(prefix="/api/merchants", tags=["merchants"])


def merchant_out(m: Merchant) -> dict:
    return {
        "merchant_id": str(m.id),
        "name": m.name,
        "phone": m.phone,
        "upi_vpa": m.upi_vpa,
        "language": m.language,
        "large_amount_threshold": format_rupees(m.large_amount_threshold_paise),
        "hold_grace_days": m.hold_grace_days,
    }


@router.post("", status_code=201)
async def create_merchant(body: MerchantIn, session: AsyncSession = Depends(get_session)):
    m = await ledger.create_merchant(
        session, body.name, body.phone, body.upi_vpa, body.language,
        rupees_to_paise(body.large_amount_threshold_rupees),
    )
    await session.commit()
    return merchant_out(m)


@router.get("/{merchant_id}")
async def get_merchant(merchant_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return merchant_out(await ledger.get_merchant(session, merchant_id))


@router.patch("/{merchant_id}")
async def update_merchant(merchant_id: uuid.UUID, body: MerchantUpdate, session: AsyncSession = Depends(get_session)):
    """Change shop settings, e.g. the language the app, voice replies and reminders use."""
    m = await ledger.get_merchant(session, merchant_id)
    fields = body.model_dump(exclude_unset=True)
    if "large_amount_threshold_rupees" in fields:
        threshold = fields.pop("large_amount_threshold_rupees")
        if threshold is not None:
            m.large_amount_threshold_paise = rupees_to_paise(threshold)
    for key, value in fields.items():
        if key in ("name", "language", "hold_grace_days") and value is None:
            continue
        setattr(m, key, value)
    await session.commit()
    return merchant_out(m)
