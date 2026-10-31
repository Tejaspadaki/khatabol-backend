"""Customer notifications (plan.md §18): every change to a customer's khata reaches them.

Flow:
  1. Ledger writes note what happened in `session.info["ledger_events"]` (service._append, edits, disputes).
  2. `capture()` turns those into `notifications` rows in the SAME transaction as the ledger write, so a
     saved entry always has its notification recorded.
  3. After commit, `dispatch_due()` sends what is due and records each attempt in notification_deliveries
     (the notification row itself never changes). So every notification is recorded before it is sent.

Rules: batching of udhaar-type entries (the first message goes at once; more within 15 minutes go out
together at the end of that window; payment receipts always go at once), quiet hours 9 PM to 8 AM (queued, never dropped), a daily digest for customers who
chose it (edits, reversals and disputes always go instantly), and an opt-out line in the first message.
"""

import uuid
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Customer,
    CustomerAccount,
    Dispute,
    EntryType,
    LedgerEntry,
    Merchant,
    Notification,
    NotificationDelivery,
    as_utc,
    utcnow,
)
from app.i18n import t
from app.nlp.hinglish_numbers import format_rupees
from app.notify.providers import channels
from app.speech import spoken_date

INSTANT = {"EDIT", "REVERSAL", "DISPUTE"}  # never held for the digest, never batched
BATCHED = {"CREDIT", "WAIVER", "POINTS"}  # udhaar-type entries are combined; payment receipts go at once


# ---------------------------------------------------------------- phones and accounts

def normalize_phone(raw: str | None) -> str | None:
    digits = "".join(ch for ch in raw or "" if ch.isdigit())
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return None


async def account_for_phone(session: AsyncSession, phone: str | None, name: str | None = None,
                            create: bool = True) -> CustomerAccount | None:
    e164 = normalize_phone(phone)
    if e164 is None:
        return None
    acc = (await session.execute(select(CustomerAccount).where(CustomerAccount.phone == e164))).scalar_one_or_none()
    if acc is None and create:
        acc = CustomerAccount(phone=e164, name=name)
        session.add(acc)
        await session.flush()
    return acc


# ---------------------------------------------------------------- events from the ledger

def note_change(session: AsyncSession, *event) -> None:
    session.info.setdefault("ledger_events", []).append(event)


def _passbook_link() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/passbook"


def _local(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(get_settings().tz)


def _utc(when: datetime | None) -> datetime:
    """Store and compare in UTC: SQLite keeps timestamps without their offset."""
    return as_utc(when or utcnow()).astimezone(timezone.utc)


def _after_quiet_hours(when: datetime) -> datetime:
    s = get_settings()
    local = _local(when)
    if local.hour >= s.quiet_start_hour:
        return _utc(datetime.combine(local.date() + timedelta(days=1), time(s.quiet_end_hour), tzinfo=s.tz))
    if local.hour < s.quiet_end_hour:
        return _utc(datetime.combine(local.date(), time(s.quiet_end_hour), tzinfo=s.tz))
    return _utc(when)


def _balance_text(paise: int, lang: str) -> str:
    if paise > 0:
        return t(lang, "notif_due", amount=format_rupees(paise))
    if paise < 0:
        return t(lang, "notif_advance", amount=format_rupees(-paise))
    return t(lang, "notif_clear")


async def _unsent(session: AsyncSession, customer_id: uuid.UUID) -> list[Notification]:
    sent = select(NotificationDelivery.notification_id)
    stmt = (select(Notification)
            .where(Notification.customer_id == customer_id, Notification.status == "QUEUED", Notification.id.not_in(sent))
            .order_by(Notification.created_at))
    return list((await session.execute(stmt)).scalars())


async def _record(session: AsyncSession, merchant: Merchant, customer: Customer, event_type: str, message: str,
                  entry_id: uuid.UUID | None, now: datetime) -> Notification:
    lang = merchant.language
    first = (await session.execute(select(Notification.id).where(Notification.customer_id == customer.id).limit(1))
             ).first() is None
    if first:
        message += " " + t(lang, "notif_consent")
    n = Notification(merchant_id=merchant.id, customer_id=customer.id, event_type=event_type, ledger_entry_id=entry_id,
                     message=message, send_after=now, status="QUEUED", created_at=now)
    if normalize_phone(customer.phone) is None:
        n.status = "NO_PHONE"
    elif customer.notify_pref == "off":
        n.status = "OPTED_OUT"
    elif customer.notify_pref == "digest" and event_type not in INSTANT:
        n.status = "DIGEST"
    elif event_type in BATCHED:
        # Batching: join a batch still waiting to go; else, if a message went out in the last 15 minutes,
        # start a batch that goes at the end of that window; else send now.
        window = timedelta(minutes=get_settings().batch_minutes)
        waiting = [x for x in await _unsent(session, customer.id) if x.batch_id and x.event_type in BATCHED]
        recent = (await session.execute(
            select(Notification).where(Notification.customer_id == customer.id,
                                       Notification.event_type.in_(BATCHED),
                                       Notification.created_at >= now - window)
            .order_by(Notification.created_at.desc()).limit(1))).scalar_one_or_none()
        if waiting:
            n.batch_id, n.send_after = waiting[-1].batch_id, as_utc(waiting[-1].send_after)
        elif recent is not None:
            n.batch_id, n.send_after = uuid.uuid4(), as_utc(recent.created_at) + window
        else:
            n.batch_id = uuid.uuid4()
    if n.status == "QUEUED":
        n.send_after = _after_quiet_hours(n.send_after)
    session.add(n)
    await session.flush()
    return n


def _entry_message(merchant: Merchant, customer: Customer, e: LedgerEntry, lang: str) -> tuple[str, str]:
    shop, link, bal = merchant.name, _passbook_link(), _balance_text(customer.balance_paise, lang)
    amount = format_rupees(e.amount_paise)
    if e.type == EntryType.CREDIT:
        due = t(lang, "notif_due_by", date=spoken_date(e.due_date, None, lang)) if e.due_date else ""
        return "CREDIT", t(lang, "notif_credit", shop=shop, amount=amount, due=due, balance=bal, link=link)
    if e.type == EntryType.PAYMENT:
        kind = "PARTIAL_PAYMENT" if customer.balance_paise > 0 else "PAYMENT"
        return kind, t(lang, "notif_payment", shop=shop, amount=amount, balance=bal)
    return "WAIVER", t(lang, "notif_waiver", shop=shop, amount=amount, reason=e.reason or "", balance=bal)


async def capture(session: AsyncSession, now: datetime | None = None) -> list[Notification]:
    """Turn this transaction's ledger events into notification rows. Call just before commit."""
    events = session.info.pop("ledger_events", [])
    if not events:
        return []
    now = _utc(now)
    in_edit = {i for ev in events if ev[0] in ("edit", "cancel") for i in ev[2:4] if i}
    out = []
    for ev in events:
        kind = ev[0]
        if kind == "entry":
            if ev[1] in in_edit:
                continue
            e = await session.get(LedgerEntry, ev[1])
            merchant = await session.get(Merchant, e.merchant_id)
            customer = await session.get(Customer, e.customer_id)
            if e.type == EntryType.REVERSAL:
                original = await session.get(LedgerEntry, e.reverses_entry_id)
                event_type, msg = "REVERSAL", t(merchant.language, "notif_reversal", shop=merchant.name,
                                                amount=format_rupees(original.amount_paise),
                                                balance=_balance_text(customer.balance_paise, merchant.language),
                                                link=_passbook_link())
            else:
                event_type, msg = _entry_message(merchant, customer, e, merchant.language)
            out.append(await _record(session, merchant, customer, event_type, msg, e.id, now))
            if e.taken_by_customer_id and e.taken_by_customer_id != e.customer_id and e.type == EntryType.CREDIT:
                member = await session.get(Customer, e.taken_by_customer_id)
                if normalize_phone(member.phone):
                    out.append(await _record(session, merchant, member, event_type, msg, e.id, now))
        elif kind in ("edit", "cancel"):
            original = await session.get(LedgerEntry, ev[1])
            merchant = await session.get(Merchant, original.merchant_id)
            customer = await session.get(Customer, original.customer_id)
            lang = merchant.language
            on = _local(original.created_at).date()
            date_txt = spoken_date(on, None, lang)
            if kind == "cancel":
                msg = t(lang, "notif_cancel", shop=merchant.name, date=date_txt, amount=format_rupees(original.amount_paise),
                        balance=_balance_text(customer.balance_paise, lang), link=_passbook_link())
                out.append(await _record(session, merchant, customer, "REVERSAL", msg, ev[2], now))
                continue
            new = await session.get(LedgerEntry, ev[3])
            changes = ", ".join(f"{c[0]}: {c[1] or '-'} → {c[2] or '-'}" for c in ev[4])
            msg = t(lang, "notif_edit", shop=merchant.name, date=date_txt, changes=changes,
                    balance=_balance_text(customer.balance_paise, lang), link=_passbook_link())
            out.append(await _record(session, merchant, customer, "EDIT", msg, new.id, now))
            if new.customer_id != original.customer_id:  # moved: the new customer is told too
                other = await session.get(Customer, new.customer_id)
                event_type, msg = _entry_message(merchant, other, new, lang)
                out.append(await _record(session, merchant, other, event_type, msg, new.id, now))
        elif kind == "dispute":
            d = await session.get(Dispute, ev[1])
            merchant = await session.get(Merchant, d.merchant_id)
            customer = await session.get(Customer, d.customer_id)
            e = await session.get(LedgerEntry, d.ledger_entry_id)
            msg = t(merchant.language, f"notif_dispute_{d.status.lower()}", shop=merchant.name,
                    amount=format_rupees(e.amount_paise), link=_passbook_link())
            out.append(await _record(session, merchant, customer, "DISPUTE", msg, e.id, now))
        elif kind == "raw":  # (raw, customer_id, event_type, message, entry_id) e.g. points (§23)
            customer = await session.get(Customer, ev[1])
            merchant = await session.get(Merchant, customer.merchant_id)
            out.append(await _record(session, merchant, customer, ev[2], ev[3], ev[4], now))
    return out


async def commit(session: AsyncSession, now: datetime | None = None) -> None:
    """Record notifications with the ledger write, commit, then send what is due."""
    await capture(session, now)
    await session.commit()
    await dispatch_due(session, now)


# ---------------------------------------------------------------- sending

_SHORT = {"CREDIT": "short_credit", "PAYMENT": "short_payment", "PARTIAL_PAYMENT": "short_payment",
          "WAIVER": "short_waiver", "POINTS": "short_points"}


async def _combined(session: AsyncSession, group: list[Notification]) -> str:
    if len(group) == 1:
        return group[0].message
    merchant = await session.get(Merchant, group[0].merchant_id)
    customer = await session.get(Customer, group[0].customer_id)
    lang, parts = merchant.language, []
    for n in group:
        e = await session.get(LedgerEntry, n.ledger_entry_id) if n.ledger_entry_id else None
        key = _SHORT.get(n.event_type)
        parts.append(t(lang, key, amount=format_rupees(e.amount_paise)) if key and e else n.message)
    return t(lang, "notif_batch", shop=merchant.name, n=len(group), lines=", ".join(parts),
             balance=_balance_text(customer.balance_paise, lang), link=_passbook_link())


async def dispatch_due(session: AsyncSession, now: datetime | None = None) -> int:
    """Send notifications whose time has come (one message per batch). WhatsApp first, then SMS."""
    now = _utc(now)
    sent_ids = select(NotificationDelivery.notification_id)
    stmt = (select(Notification).where(Notification.status == "QUEUED", Notification.send_after <= now,
                                       Notification.id.not_in(sent_ids)).order_by(Notification.created_at))
    groups: dict = defaultdict(list)
    for n in (await session.execute(stmt)).scalars():
        groups[n.batch_id or n.id].append(n)
    primary, fallback = channels()
    for group in groups.values():
        customer = await session.get(Customer, group[0].customer_id)
        phone, text = normalize_phone(customer.phone), await _combined(session, group)
        result, used = await primary.send(phone, text), primary.channel
        attempts = [(primary.channel, result)]
        if not result.ok:
            result, used = await fallback.send(phone, text), fallback.channel
            attempts.append((fallback.channel, result))
        for n in group:
            for channel, r in attempts:
                session.add(NotificationDelivery(notification_id=n.id, channel=channel,
                                                 status="SENT" if r.ok else "FAILED", detail=r.ref or r.error))
    await session.commit()
    return len(groups)


async def send_digests(session: AsyncSession, now: datetime | None = None) -> int:
    """Daily: one summary per customer who chose the digest, for the changes held for it."""
    now = _utc(now)
    done = select(NotificationDelivery.notification_id)
    stmt = select(Notification).where(Notification.status == "DIGEST", Notification.id.not_in(done))
    per_customer: dict = defaultdict(list)
    for n in (await session.execute(stmt)).scalars():
        per_customer[n.customer_id].append(n)
    for customer_id, items in per_customer.items():
        customer = await session.get(Customer, customer_id)
        merchant = await session.get(Merchant, customer.merchant_id)
        lang = merchant.language
        msg = t(lang, "notif_digest", shop=merchant.name, n=len(items),
                balance=_balance_text(customer.balance_paise, lang), link=_passbook_link())
        digest = Notification(merchant_id=merchant.id, customer_id=customer_id, event_type="DIGEST", message=msg,
                              status="QUEUED", send_after=_after_quiet_hours(now))
        session.add(digest)
        await session.flush()
        for n in items:
            session.add(NotificationDelivery(notification_id=n.id, channel="digest", status="DIGESTED",
                                             detail=str(digest.id)))
    await session.commit()
    await dispatch_due(session, now)
    return len(per_customer)


async def delivery_status(session: AsyncSession, n: Notification) -> str:
    rows = list((await session.execute(select(NotificationDelivery).where(NotificationDelivery.notification_id == n.id)
                                       .order_by(NotificationDelivery.created_at))).scalars())
    if any(r.status == "SENT" for r in rows):
        return "SENT"
    return rows[-1].status if rows else n.status


async def notification_card(session: AsyncSession, n: Notification) -> dict:
    return {"notification_id": str(n.id), "customer_id": str(n.customer_id), "event_type": n.event_type,
            "message": n.message, "status": await delivery_status(session, n),
            "send_after": as_utc(n.send_after).isoformat(), "at": as_utc(n.created_at).isoformat(),
            "batch_id": str(n.batch_id) if n.batch_id else None}

