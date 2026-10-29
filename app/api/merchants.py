import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.api.schemas import MerchantIn
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
