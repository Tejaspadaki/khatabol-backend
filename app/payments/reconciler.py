"""UPI auto-reconciliation (plan.md §5).

incoming payment -> idempotency check -> match payer (VPA, then KHATA- ref in note)
        -> APPLIED (auto-settle) | SALE (no khata change) | UNMATCHED (ask merchant once)
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Customer,
    CustomerUpiHandle,
    EntrySource,
    LedgerEntry,
    PaymentMode,
    UpiTransaction,
    UpiTxnStatus,
)
from app.events import bus
from app.i18n import t
from app.ledger import service as ledger
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.hinglish_numbers import format_rupees
from app.payments.upi_links import ref_from_note
from app.speech import balance_phrase


@dataclass
class PaymentEvent:
    merchant_id: uuid.UUID
    txn_id: str
    payer_vpa: str
    amount_paise: int
    payer_name: str | None = None
    note: str | None = None
    paid_at: datetime | None = None


@dataclass
class ReconcileResult:
    txn: UpiTransaction
    duplicate: bool
    announcement: str | None
    event_type: str | None = None
    customer: Customer | None = None

    def publish(self) -> None:
        """Call after commit so nobody hears about a payment that was rolled back."""
        if self.announcement and self.event_type and not self.duplicate:
            data = {"txn_id": self.txn.txn_id, "payer_vpa": self.txn.payer_vpa,
                    "amount_paise": self.txn.amount_paise, "status": self.txn.status.value}
            if self.customer:
                data["customer_id"] = str(self.customer.id)
            bus.publish(self.txn.merchant_id, self.event_type, self.announcement, **data)


async def _get_txn(session: AsyncSession, merchant_id: uuid.UUID, txn_id: str) -> UpiTransaction:
    stmt = select(UpiTransaction).where(UpiTransaction.merchant_id == merchant_id, UpiTransaction.txn_id == txn_id)
    txn = (await session.execute(stmt)).scalar_one_or_none()
    if txn is None:
        raise NotFoundError.of("err_payment_not_found")
    return txn


async def _has_any_dues(session: AsyncSession, merchant_id: uuid.UUID) -> bool:
    stmt = select(exists().where(Customer.merchant_id == merchant_id, Customer.balance_paise > 0))
    return bool((await session.execute(stmt)).scalar())


async def handle_payment(session: AsyncSession, event: PaymentEvent) -> ReconcileResult:
    """Caller commits. A concurrent duplicate fails on the txn_id UNIQUE constraint."""
    if event.amount_paise <= 0:
        raise LedgerError.of("err_payment_positive")
    stmt = select(UpiTransaction).where(UpiTransaction.txn_id == event.txn_id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing:
        return ReconcileResult(existing, duplicate=True, announcement=None)

    vpa = event.payer_vpa.strip().lower()
    txn = UpiTransaction(
        merchant_id=event.merchant_id,
        txn_id=event.txn_id,
        payer_vpa=vpa,
        payer_name=event.payer_name,
        amount_paise=event.amount_paise,
        note=event.note,
        status=UpiTxnStatus.SALE,
    )
    if event.paid_at:
        txn.paid_at = event.paid_at
    session.add(txn)

    customer = await ledger.customer_for_vpa(session, event.merchant_id, vpa)
    if customer is None and (ref := ref_from_note(event.note)):
        customer = await ledger.customer_for_ref(session, event.merchant_id, ref)
        if customer:  # paid from our reminder link: learn this VPA
            await ledger.link_upi(session, event.merchant_id, customer.id, vpa, linked_via="payment_link", verified=True)

    amount = format_rupees(event.amount_paise)
    lang = (await ledger.get_merchant(session, event.merchant_id)).language
    if customer is not None:
        txn.customer_id = customer.id
        if customer.balance_paise > 0:
            entry = await ledger.record_payment(
                session,
                event.merchant_id,
                customer.id,
                event.amount_paise,
                mode=PaymentMode.UPI,
                source=EntrySource.UPI_AUTO,
                upi_txn_id=event.txn_id,
                note=f"UPI from {vpa}",
            )
            txn.status = UpiTxnStatus.APPLIED
            txn.ledger_entry_id = entry.id
            balance = balance_phrase(customer.balance_paise, lang).capitalize()
            text = t(lang, "upi_applied", name=customer.name, amount=amount, balance=balance)
            await session.flush()
            return ReconcileResult(txn, False, text, "upi_applied", customer)
        text = t(lang, "upi_sale_from", amount=amount, name=customer.name)
        await session.flush()
        return ReconcileResult(txn, False, text, "upi_sale", customer)

    if await _has_any_dues(session, event.merchant_id):
        txn.status = UpiTxnStatus.UNMATCHED
        text = t(lang, "upi_unmatched", amount=amount, vpa=vpa)
        await session.flush()
        return ReconcileResult(txn, False, text, "upi_unmatched")

    text = t(lang, "upi_sale", amount=amount)
    await session.flush()
    return ReconcileResult(txn, False, text, "upi_sale")


async def assign_payment(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    txn_id: str,
    customer_id: uuid.UUID,
    source: EntrySource = EntrySource.MANUAL,
    voice_log_id: uuid.UUID | None = None,
) -> tuple[UpiTransaction, Customer]:
    """Merchant answers "whose payment is this?". Links the VPA so it auto-matches next time."""
    txn = await _get_txn(session, merchant_id, txn_id)
    if txn.status in (UpiTxnStatus.APPLIED, UpiTxnStatus.RESOLVED):
        raise LedgerError.of("err_payment_linked")
    customer = await ledger.get_customer(session, merchant_id, customer_id)
    await ledger.link_upi(session, merchant_id, customer_id, txn.payer_vpa, linked_via="merchant_answer")
    # After an undo, the reversed entry still holds this txn_id (UNIQUE), so don't reuse it.
    used = (await session.execute(select(exists().where(LedgerEntry.upi_txn_id == txn.txn_id)))).scalar()
    entry = await ledger.record_payment(
        session,
        merchant_id,
        customer_id,
        txn.amount_paise,
        mode=PaymentMode.UPI,
        source=source,
        upi_txn_id=None if used else txn.txn_id,
        note=f"UPI {txn.txn_id} from {txn.payer_vpa} (assigned by merchant)",
        voice_log_id=voice_log_id,
    )
    txn.status = UpiTxnStatus.RESOLVED
    txn.customer_id = customer_id
    txn.ledger_entry_id = entry.id
    await session.flush()
    return txn, customer


async def undo_auto_payment(session: AsyncSession, merchant_id: uuid.UUID, txn_id: str) -> UpiTransaction:
    """"Ye Ramesh ka nahi tha": reverse the entry, forget the VPA link, ask again."""
    txn = await _get_txn(session, merchant_id, txn_id)
    if txn.ledger_entry_id is None:
        raise LedgerError.of("err_payment_no_entry")
    await ledger.reverse_entry(session, merchant_id, txn.ledger_entry_id, note="UPI payment wrongly matched")
    handle_stmt = select(CustomerUpiHandle).where(
        CustomerUpiHandle.merchant_id == merchant_id, CustomerUpiHandle.vpa == txn.payer_vpa
    )
    handle = (await session.execute(handle_stmt)).scalar_one_or_none()
    if handle:
        await session.delete(handle)
    txn.status = UpiTxnStatus.UNMATCHED
    txn.customer_id = None
    txn.ledger_entry_id = None
    await session.flush()
    return txn


async def list_unmatched(session: AsyncSession, merchant_id: uuid.UUID) -> list[UpiTransaction]:
    stmt = (
        select(UpiTransaction)
        .where(UpiTransaction.merchant_id == merchant_id, UpiTransaction.status == UpiTxnStatus.UNMATCHED)
        .order_by(UpiTransaction.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars())
