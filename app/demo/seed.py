"""Demo data: one kirana store with the customers from the plan's demo script."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import EntrySource, Merchant, PaymentMode
from app.ledger import service as ledger


async def seed_demo(session: AsyncSession) -> Merchant:
    tz = get_settings().tz
    now = datetime.now(tz)
    today = now.date()

    m = await ledger.create_merchant(session, "Sharma General Store", "9876500000", "sharmastore@paytm")

    async def backdate(entry, days_ago: int):
        entry.created_at = now - timedelta(days=days_ago)
        await session.flush()

    ramesh_p = await ledger.create_customer(session, m.id, "Ramesh Patil", "9822001234")
    ramesh_s = await ledger.create_customer(session, m.id, "Ramesh Sharma", "9822005678")
    sunita = await ledger.create_customer(session, m.id, "Sunita Devi", "9890011122", ["sunita didi"])
    mohan = await ledger.create_customer(session, m.id, "Mohan Lal", None, ["mohan chacha"])
    await ledger.create_customer(session, m.id, "Pappu", "9000012345", ["pappu chai wala"])

    await ledger.link_upi(session, m.id, ramesh_p.id, "ramesh.p@okaxis", linked_via="manual", verified=True)

    e = await ledger.add_credit(session, m.id, ramesh_p.id, 70000, today - timedelta(days=2), "aata, tel",
                                source=EntrySource.MANUAL)
    await backdate(e, 9)
    e = await ledger.record_payment(session, m.id, ramesh_p.id, 20000, PaymentMode.CASH, EntrySource.MANUAL)
    await backdate(e, 9)
    e = await ledger.add_credit(session, m.id, ramesh_s.id, 120000, today + timedelta(days=4), "chawal 10kg",
                                source=EntrySource.MANUAL)
    await backdate(e, 3)
    e = await ledger.add_credit(session, m.id, sunita.id, 45000, today, "doodh, sabzi", source=EntrySource.MANUAL)
    await backdate(e, 6)
    e = await ledger.add_credit(session, m.id, mohan.id, 25000, None, "cigarette, chai", source=EntrySource.MANUAL)
    await backdate(e, 1)
    return m
