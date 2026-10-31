""""Galat hai" disputes (plan.md §18.4). While a dispute is open, reminders (and calls, §21) skip that
entry; the merchant is told and resolves it by confirming the entry or editing it (§19)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, Dispute, LedgerEntry, Merchant, utcnow
from app.events import bus
from app.i18n import t
from app.ledger import service as ledger
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.hinglish_numbers import format_rupees
from app.notify.service import note_change


async def open_dispute(session: AsyncSession, entry: LedgerEntry, raised_by: str, reason: str | None) -> Dispute:
    existing = (await session.execute(select(Dispute).where(Dispute.ledger_entry_id == entry.id,
                                                            Dispute.status == "OPEN"))).scalar_one_or_none()
    if existing:
        return existing
    d = Dispute(merchant_id=entry.merchant_id, customer_id=entry.customer_id, ledger_entry_id=entry.id,
                raised_by=raised_by, reason=" ".join((reason or "").split()) or None)
    session.add(d)
    await session.flush()
    customer = await session.get(Customer, entry.customer_id)
    merchant = await session.get(Merchant, entry.merchant_id)
    await ledger.record_event(session, customer, "DISPUTE_OPENED",
                              {"dispute_id": str(d.id), "entry_id": str(entry.id), "reason": d.reason},
                              "customer" if raised_by in ("customer", "call") else "merchant")
    bus.publish(merchant.id, "dispute_opened",
                t(merchant.language, "dispute_opened", name=customer.name, amount=format_rupees(entry.amount_paise)),
                dispute_id=str(d.id), customer_id=str(customer.id))
    return d


async def list_disputes(session: AsyncSession, merchant_id: uuid.UUID, status: str | None = "OPEN") -> list[Dispute]:
    stmt = select(Dispute).where(Dispute.merchant_id == merchant_id).order_by(Dispute.created_at)
    if status:
        stmt = stmt.where(Dispute.status == status)
    return list((await session.execute(stmt)).scalars())


async def confirm_dispute(session: AsyncSession, merchant_id: uuid.UUID, dispute_id: uuid.UUID,
                          note: str | None = None) -> Dispute:
    """The merchant says the entry is right. The customer is told."""
    d = await session.get(Dispute, dispute_id)
    if d is None or d.merchant_id != merchant_id:
        raise NotFoundError.of("err_dispute_not_found")
    if d.status != "OPEN":
        raise LedgerError.of("err_dispute_closed")
    d.status, d.resolved_at, d.resolution_note = "CONFIRMED", utcnow(), note
    customer = await session.get(Customer, d.customer_id)
    await ledger.record_event(session, customer, "DISPUTE_CONFIRMED", {"dispute_id": str(d.id)}, "merchant")
    note_change(session, "dispute", d.id)
    return d


async def resolve_by_edit(session: AsyncSession, original_entry_id: uuid.UUID) -> None:
    """An edit or cancel of a disputed entry resolves the dispute (status EDITED)."""
    for d in (await session.execute(select(Dispute).where(Dispute.ledger_entry_id == original_entry_id,
                                                          Dispute.status == "OPEN"))).scalars():
        d.status, d.resolved_at, d.resolution_note = "EDITED", utcnow(), "entry edited"
        customer = await session.get(Customer, d.customer_id)
        await ledger.record_event(session, customer, "DISPUTE_EDITED", {"dispute_id": str(d.id)}, "merchant")


async def disputed_remaining(session: AsyncSession, customer_id: uuid.UUID) -> int:
    """What is still owed on entries under an open dispute: reminders and calls leave it out."""
    open_ids = set((await session.execute(select(Dispute.ledger_entry_id).where(
        Dispute.customer_id == customer_id, Dispute.status == "OPEN"))).scalars())
    if not open_ids:
        return 0
    return sum(c.remaining_paise for c in await ledger.open_credits_for(session, customer_id) if c.entry.id in open_ids)


async def open_dispute_ids(session: AsyncSession, customer_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    if not customer_ids:
        return set()
    stmt = select(Dispute.ledger_entry_id).where(Dispute.customer_id.in_(customer_ids), Dispute.status == "OPEN")
    return set((await session.execute(stmt)).scalars())
