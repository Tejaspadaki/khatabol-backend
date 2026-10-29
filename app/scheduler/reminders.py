"""Payment reminders with a one-tap UPI link. Delivery is mocked (logged + stored) for the demo."""

import logging
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer, Merchant, Reminder, utcnow
from app.ledger.errors import LedgerError
from app.nlp.hinglish_numbers import format_rupees
from app.payments.upi_links import upi_intent_link
from app.speech import spoken_date

log = logging.getLogger("khata.reminders")


def reminder_message(merchant: Merchant, customer: Customer, amount_paise: int, link: str,
                     due: date | None = None) -> str:
    due_txt = f" ({spoken_date(due)} tak)" if due else ""
    return (
        f"Namaste {customer.name} ji, {merchant.name} se yaad dila rahe hain: aapka "
        f"{format_rupees(amount_paise)} baaki hai{due_txt}. Yahan se pay karein: {link}"
    )


async def send_reminders(
    session: AsyncSession, merchant: Merchant, targets: list[tuple[Customer, int, date | None]],
    channel: str = "whatsapp",
) -> list[Reminder]:
    """targets: (customer, amount to ask for, due date). Caller commits."""
    if not merchant.upi_vpa:
        raise LedgerError("Reminder ke liye pehle apni UPI ID set kijiye.")
    sent = []
    for customer, amount, due in targets:
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
        )
        session.add(r)
        sent.append(r)
    await session.flush()
    return sent
