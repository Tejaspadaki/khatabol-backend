"""Customer passbook (plan.md §18.3): OTP login by phone, then every shop's khata for that person only."""

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Customer, CustomerAccount, LedgerEntry, Merchant, PassbookOtp, PassbookSession, as_utc, utcnow
from app.i18n import t
from app.ledger import service as ledger
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.hinglish_numbers import format_rupees
from app.notify import disputes
from app.notify.providers import channels
from app.notify.service import account_for_phone

MAX_OTP_ATTEMPTS = 5


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PassbookAuthError(LedgerError):
    pass


async def request_otp(session: AsyncSession, phone: str) -> None:
    """Send a 6-digit code by SMS. Says nothing about whether the number has a khata (no enumeration)."""
    acc = await account_for_phone(session, phone, create=False)
    if acc is None:
        return
    code = f"{secrets.randbelow(10**6):06d}"
    session.add(PassbookOtp(account_id=acc.id, code_hash=_hash(f"{acc.id}:{code}"),
                            expires_at=utcnow() + timedelta(seconds=get_settings().otp_ttl_seconds)))
    await session.commit()
    _, sms = channels()
    await sms.send(acc.phone, t(acc.preferred_language, "otp_message", code=code))


async def verify_otp(session: AsyncSession, phone: str, code: str) -> str:
    """Single use, expires, at most 5 tries. Returns a session token (only its hash is stored)."""
    acc = await account_for_phone(session, phone, create=False)
    if acc is None:
        raise PassbookAuthError.of("err_otp_wrong")
    otp = (await session.execute(select(PassbookOtp).where(PassbookOtp.account_id == acc.id, PassbookOtp.used_at.is_(None))
                                 .order_by(PassbookOtp.created_at.desc()).limit(1))).scalar_one_or_none()
    if otp is None or as_utc(otp.expires_at) < utcnow() or otp.attempts >= MAX_OTP_ATTEMPTS:
        raise PassbookAuthError.of("err_otp_expired")
    otp.attempts += 1
    if not hmac.compare_digest(otp.code_hash, _hash(f"{acc.id}:{code.strip()}")):
        await session.commit()
        raise PassbookAuthError.of("err_otp_wrong")
    otp.used_at = utcnow()
    token = secrets.token_urlsafe(32)
    session.add(PassbookSession(account_id=acc.id, token_hash=_hash(token),
                                expires_at=utcnow() + timedelta(hours=get_settings().passbook_session_hours)))
    await session.commit()
    return token


async def account_for_token(session: AsyncSession, token: str | None) -> CustomerAccount:
    if not token:
        raise PassbookAuthError.of("err_passbook_login")
    s = (await session.execute(select(PassbookSession).where(PassbookSession.token_hash == _hash(token)))
         ).scalar_one_or_none()
    if s is None or as_utc(s.expires_at) < utcnow():
        raise PassbookAuthError.of("err_passbook_login")
    return await session.get(CustomerAccount, s.account_id)


async def _customers(session: AsyncSession, acc: CustomerAccount) -> list[Customer]:
    return list((await session.execute(select(Customer).where(Customer.customer_account_id == acc.id))).scalars())


async def view(session: AsyncSession, acc: CustomerAccount, today) -> dict:
    """Balance per shop and in total, every entry with its status and due date, points, pay links."""
    from app.payments.upi_links import upi_intent_link
    from app.agent.tools import entry_card

    customers = await _customers(session, acc)
    disputed = await disputes.open_dispute_ids(session, [c.id for c in customers])
    shops, total = [], 0
    for c in customers:
        m = await session.get(Merchant, c.merchant_id)
        st = await ledger.entry_statuses(session, c.id)
        entries = await ledger.get_history(session, m.id, c.id, 100)
        total += max(c.balance_paise, 0)
        shops.append({
            "merchant_id": str(m.id), "shop": m.name, "balance": format_rupees(c.balance_paise),
            "balance_paise": c.balance_paise, "points": await _points(session, c),
            "notify_pref": c.notify_pref,
            "pay_link": upi_intent_link(m.upi_vpa, m.name, c.balance_paise, c.ref_code)
            if m.upi_vpa and c.balance_paise > 0 else None,
            "entries": [{**entry_card(e, status=st.get(e.id)), "disputed": e.id in disputed} for e in entries],
        })
    return {"name": acc.name, "phone": acc.phone[:3] + "•" * (len(acc.phone) - 7) + acc.phone[-4:],
            "total_due": format_rupees(total), "total_due_paise": total,
            "points": sum(s["points"] for s in shops), "shops": shops}


async def _points(session: AsyncSession, c: Customer) -> int:
    try:
        from app.loyalty import service as loyalty
    except ImportError:  # loyalty (§23) not installed
        return 0
    return await loyalty.balance(session, c)


async def raise_dispute(session: AsyncSession, acc: CustomerAccount, entry_id: uuid.UUID, reason: str | None):
    e = await session.get(LedgerEntry, entry_id)
    if e is None or e.customer_id not in {c.id for c in await _customers(session, acc)}:
        raise NotFoundError.of("err_entry_not_found")  # never reveal other people's entries
    d = await disputes.open_dispute(session, e, "customer", reason)
    await session.commit()
    return d


async def set_pref(session: AsyncSession, acc: CustomerAccount, merchant_id: uuid.UUID, mode: str) -> None:
    if mode not in ("instant", "digest", "off"):
        raise LedgerError("mode must be instant, digest or off.")
    match = [c for c in await _customers(session, acc) if c.merchant_id == merchant_id]
    if not match:
        raise NotFoundError.of("err_customer_not_found")
    match[0].notify_pref = mode
    await session.commit()
