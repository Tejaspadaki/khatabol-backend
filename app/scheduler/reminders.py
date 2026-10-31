"""Payment reminders with a one-tap UPI link. Delivery is mocked (logged + stored) for the demo."""

import logging
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, Merchant, Reminder, utcnow
from app.i18n import t
from app.ledger.errors import LedgerError
from app.nlp.hinglish_numbers import format_rupees
from app.payments.upi_links import upi_intent_link
from app.speech import spoken_date

log = logging.getLogger("khata.reminders")


def reminder_message(merchant: Merchant, customer: Customer, amount_paise: int, link: str,
                     due: date | None = None) -> str:
    lang = merchant.language
    due_txt = t(lang, "reminder_due", date=spoken_date(due, lang=lang)) if due else ""
    return t(lang, "reminder_message", customer=customer.name, shop=merchant.name,
             amount=format_rupees(amount_paise), due=due_txt, link=link)


async def send_reminders(
    session: AsyncSession, merchant: Merchant, targets: list[tuple[Customer, int, date | None]],
    channel: str = "whatsapp", source_payment_id: uuid.UUID | None = None, nudge_date: date | None = None,
) -> list[Reminder]:
    """targets: (customer, amount to ask for, due date). Caller commits."""
    if not merchant.upi_vpa:
        raise LedgerError.of("err_reminder_needs_upi")
    from app.notify.disputes import disputed_remaining

    sent = []
    for customer, amount, due in targets:
        amount -= await disputed_remaining(session, customer.id)  # an open dispute pauses that entry
        if amount <= 0:
            continue
        link = upi_intent_link(merchant.upi_vpa, merchant.name, amount, customer.ref_code)
        msg = reminder_message(merchant, customer, amount, link, due)
        # Swap for the WhatsApp Business / SMS gateway call here.
        log.info("REMINDER to=%s phone=%s msg=%s", customer.name, customer.phone, msg)
        r = Reminder(
            merchant_id=merchant.id,
            customer_id=customer.id,
            channel=channel,
            message=msg,
            amount_paise=amount,
            payment_link=link,
            sent_at=utcnow(),
            status="SENT" if customer.phone else "NO_PHONE",
            source_payment_id=source_payment_id,
            nudge_date=nudge_date,
        )
        session.add(r)
        sent.append(r)
    await session.flush()
    return sent


async def nudge_sent(session: AsyncSession, payment_id: uuid.UUID, day: date) -> bool:
    stmt = select(Reminder.id).where(Reminder.source_payment_id == payment_id, Reminder.nudge_date == day)
    return (await session.execute(stmt)).first() is not None


async def send_payment_nudge(
    session: AsyncSession, merchant: Merchant, customer: Customer, payment_id: uuid.UUID, day: date,
) -> Reminder:
    """Reminder for what is still due after a partial payment. The amount is the balance right now,
    not the payment's; one per payment per day (unique key). Caller commits."""
    from app.ledger import service as ledger

    b = await ledger.get_balance(session, merchant.id, customer.id, day)
    if b["balance_paise"] <= 0:
        raise LedgerError.of("err_nothing_due", name=customer.name)
    try:
        async with session.begin_nested():
            sent = await send_reminders(session, merchant, [(customer, b["balance_paise"], b["next_due_date"])],
                                        source_payment_id=payment_id, nudge_date=day)
    except IntegrityError:
        raise LedgerError.of("err_nudge_sent_today", name=customer.name) from None
    return sent[0]
