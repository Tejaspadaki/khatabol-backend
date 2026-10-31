"""Ledger service: the ONLY module that writes khata data.

Functions flush but never commit; the caller owns the transaction.
Money is always integer paise. Positive balance = customer owes the merchant.
"""

import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import (
    Customer,
    CustomerUpiHandle,
    EntrySource,
    EntryType,
    LedgerEntry,
    Merchant,
    PaymentMode,
    as_utc,
)
from app.ledger.allocation import (
    derive_balance,
    effective_entries,
    next_due,
    open_credits,
    overdue_amount,
    signed_delta,
)
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.names import CandidateName, decide, normalize_name, phonetic_key, score_candidates

MAX_AMOUNT_PAISE = 10_000_000_00  # ₹1 crore sanity cap


# ---------------------------------------------------------------- merchants

async def create_merchant(
    session: AsyncSession,
    name: str,
    phone: str | None = None,
    upi_vpa: str | None = None,
    language: str = "hi",
    large_amount_threshold_paise: int = 1_000_000,
) -> Merchant:
    m = Merchant(
        name=name,
        phone=phone,
        upi_vpa=upi_vpa,
        language=language,
        large_amount_threshold_paise=large_amount_threshold_paise,
    )
    session.add(m)
    await session.flush()
    return m


async def get_merchant(session: AsyncSession, merchant_id: uuid.UUID) -> Merchant:
    m = await session.get(Merchant, merchant_id)
    if m is None:
        raise NotFoundError.of("err_merchant_not_found")
    return m


# ---------------------------------------------------------------- customers

async def create_customer(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    name: str,
    phone: str | None = None,
    nicknames: list[str] | None = None,
) -> Customer:
    name = " ".join(name.split()).title() if name.isascii() else " ".join(name.split())
    normalized = normalize_name(name)
    if not normalized:
        raise LedgerError.of("err_customer_name")
    c = Customer(
        merchant_id=merchant_id,
        name=name,
        name_normalized=normalized,
        phonetic_key=phonetic_key(normalized),
        phone=phone,
        nicknames=[n for n in (nicknames or []) if n],
    )
    session.add(c)
    await session.flush()
    return c


async def get_customer(
    session: AsyncSession, merchant_id: uuid.UUID, customer_id: uuid.UUID, lock: bool = False
) -> Customer:
    stmt = select(Customer).where(Customer.id == customer_id, Customer.merchant_id == merchant_id)
    if lock:
        stmt = stmt.with_for_update()
    c = (await session.execute(stmt)).scalar_one_or_none()
    if c is None:
        raise NotFoundError.of("err_customer_not_found")
    return c


async def list_customers(session: AsyncSession, merchant_id: uuid.UUID) -> list[Customer]:
    stmt = select(Customer).where(Customer.merchant_id == merchant_id).order_by(Customer.name)
    return list((await session.execute(stmt)).scalars())


async def find_customers(
    session: AsyncSession, merchant_id: uuid.UUID, query: str, phone: str | None = None, limit: int = 4
) -> dict:
    """Fuzzy + phonetic lookup. Returns {decision, matches:[{customer, score}]}."""
    customers = await list_customers(session, merchant_id)
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        by_phone = [c for c in customers if c.phone and digits and c.phone.endswith(digits[-10:])]
        if len(by_phone) == 1:
            return {"decision": "match", "matches": [{"customer": by_phone[0], "score": 1.0}]}
    by_id = {c.id: c for c in customers}
    scored = score_candidates(query, [CandidateName(c.id, c.name, c.nicknames or []) for c in customers])
    decision = decide(scored)
    keep = [m for m in scored if m.score >= 0.6][:limit]
    return {
        "decision": decision,
        "matches": [{"customer": by_id[m.customer_id], "score": m.score} for m in keep],
    }


def learn_nickname(customer: Customer, spoken_name: str | None) -> bool:
    """After the merchant picks a customer, remember how they said the name."""
    if not spoken_name:
        return False
    spoken = normalize_name(spoken_name)
    known = {customer.name_normalized, *(normalize_name(n) for n in customer.nicknames or [])}
    if not spoken or spoken in known:
        return False
    customer.nicknames = [*(customer.nicknames or []), spoken]  # reassign so JSON change is tracked
    return True


# ---------------------------------------------------------------- ledger writes

def _check_amount(amount_paise: int) -> None:
    if amount_paise <= 0:
        raise LedgerError.of("err_amount_positive")
    if amount_paise > MAX_AMOUNT_PAISE:
        raise LedgerError.of("err_amount_too_big")


async def _append(session: AsyncSession, customer: Customer, entry: LedgerEntry, delta: int) -> LedgerEntry:
    """Insert entry and update cached balance in the same transaction."""
    session.add(entry)
    customer.balance_paise = (customer.balance_paise or 0) + delta
    await session.flush()
    return entry


async def add_credit(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    customer_id: uuid.UUID,
    amount_paise: int,
    due_date: date | None = None,
    items: str | None = None,
    note: str | None = None,
    source: EntrySource = EntrySource.VOICE,
    voice_log_id: uuid.UUID | None = None,
    spoken_name: str | None = None,
) -> LedgerEntry:
    _check_amount(amount_paise)
    customer = await get_customer(session, merchant_id, customer_id, lock=True)
    learn_nickname(customer, spoken_name)
    entry = LedgerEntry(
        merchant_id=merchant_id,
        customer_id=customer_id,
        type=EntryType.CREDIT,
        amount_paise=amount_paise,
        due_date=due_date,
        items=items,
        note=note,
        source=source,
        voice_log_id=voice_log_id,
        spoken_name=spoken_name,
    )
    return await _append(session, customer, entry, amount_paise)


async def record_payment(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    customer_id: uuid.UUID,
    amount_paise: int,
    mode: PaymentMode = PaymentMode.CASH,
    source: EntrySource = EntrySource.VOICE,
    upi_txn_id: str | None = None,
    note: str | None = None,
    voice_log_id: uuid.UUID | None = None,
    spoken_name: str | None = None,
) -> LedgerEntry:
    _check_amount(amount_paise)
    customer = await get_customer(session, merchant_id, customer_id, lock=True)
    learn_nickname(customer, spoken_name)
    entry = LedgerEntry(
        merchant_id=merchant_id,
        customer_id=customer_id,
        type=EntryType.PAYMENT,
        amount_paise=amount_paise,
        payment_mode=mode,
        note=note,
        source=source,
        upi_txn_id=upi_txn_id,
        voice_log_id=voice_log_id,
        spoken_name=spoken_name,
    )
    return await _append(session, customer, entry, -amount_paise)


async def _is_reversed(session: AsyncSession, entry_id: uuid.UUID) -> bool:
    stmt = select(func.count()).select_from(LedgerEntry).where(LedgerEntry.reverses_entry_id == entry_id)
    return (await session.execute(stmt)).scalar_one() > 0


async def reverse_entry(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    entry_id: uuid.UUID,
    note: str | None = None,
    source: EntrySource = EntrySource.VOICE,
    voice_log_id: uuid.UUID | None = None,
) -> LedgerEntry:
    original = await session.get(LedgerEntry, entry_id)
    if original is None or original.merchant_id != merchant_id:
        raise NotFoundError.of("err_entry_not_found")
    if original.type == EntryType.REVERSAL:
        raise LedgerError.of("err_reverse_reversal")
    if await _is_reversed(session, entry_id):
        raise LedgerError.of("err_entry_cancelled")
    customer = await get_customer(session, merchant_id, original.customer_id, lock=True)
    entry = LedgerEntry(
        merchant_id=merchant_id,
        customer_id=original.customer_id,
        type=EntryType.REVERSAL,
        amount_paise=original.amount_paise,
        reverses_entry_id=original.id,
        note=note,
        source=source,
        voice_log_id=voice_log_id,
    )
    return await _append(session, customer, entry, -signed_delta(original, {}))


async def last_reversible_entry(session: AsyncSession, merchant_id: uuid.UUID) -> LedgerEntry | None:
    stmt = (
        select(LedgerEntry)
        .where(LedgerEntry.merchant_id == merchant_id, LedgerEntry.type != EntryType.REVERSAL)
        .order_by(LedgerEntry.created_at.desc())
        .limit(20)
    )
    for e in (await session.execute(stmt)).scalars():
        if not await _is_reversed(session, e.id):
            return e
    return None


async def link_upi(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    customer_id: uuid.UUID,
    vpa: str,
    linked_via: str = "voice",
    verified: bool = False,
) -> CustomerUpiHandle:
    vpa = vpa.strip().lower()
    if "@" not in vpa or " " in vpa:
        raise LedgerError.of("err_bad_upi")
    await get_customer(session, merchant_id, customer_id)
    stmt = select(CustomerUpiHandle).where(
        CustomerUpiHandle.merchant_id == merchant_id, CustomerUpiHandle.vpa == vpa
    )
    handle = (await session.execute(stmt)).scalar_one_or_none()
    if handle:
        # The merchant's latest word wins: re-point the VPA.
        handle.customer_id = customer_id
        handle.linked_via = linked_via
        handle.verified = handle.verified or verified
    else:
        handle = CustomerUpiHandle(
            merchant_id=merchant_id, customer_id=customer_id, vpa=vpa, linked_via=linked_via, verified=verified
        )
        session.add(handle)
    await session.flush()
    return handle


async def customer_for_vpa(session: AsyncSession, merchant_id: uuid.UUID, vpa: str) -> Customer | None:
    stmt = (
        select(Customer)
        .join(CustomerUpiHandle, CustomerUpiHandle.customer_id == Customer.id)
        .where(CustomerUpiHandle.merchant_id == merchant_id, CustomerUpiHandle.vpa == vpa.strip().lower())
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def upi_ids(session: AsyncSession, customer_id: uuid.UUID) -> list[str]:
    stmt = select(CustomerUpiHandle.vpa).where(CustomerUpiHandle.customer_id == customer_id)
    return list((await session.execute(stmt)).scalars())


async def customer_for_ref(session: AsyncSession, merchant_id: uuid.UUID, ref_code: str) -> Customer | None:
    stmt = select(Customer).where(Customer.merchant_id == merchant_id, Customer.ref_code == ref_code.upper())
    return (await session.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------- reads

async def customer_entries(session: AsyncSession, customer_id: uuid.UUID) -> list[LedgerEntry]:
    stmt = select(LedgerEntry).where(LedgerEntry.customer_id == customer_id).order_by(LedgerEntry.created_at)
    return list((await session.execute(stmt)).scalars())


async def get_balance(session: AsyncSession, merchant_id: uuid.UUID, customer_id: uuid.UUID, today: date) -> dict:
    customer = await get_customer(session, merchant_id, customer_id)
    entries = await customer_entries(session, customer_id)
    credits = open_credits(entries)
    due_date, due_amount = next_due(credits)
    payments = [e for e in effective_entries(entries) if e.type == EntryType.PAYMENT]  # skip undone ones
    last_payment = max(payments, key=lambda e: as_utc(e.created_at)) if payments else None
    return {
        "customer": customer,
        "balance_paise": customer.balance_paise,
        "next_due_date": due_date,
        "next_due_paise": due_amount,
        "overdue_paise": overdue_amount(credits, today),
        "last_payment": last_payment,
    }


async def get_history(
    session: AsyncSession, merchant_id: uuid.UUID, customer_id: uuid.UUID, limit: int = 10
) -> list[LedgerEntry]:
    await get_customer(session, merchant_id, customer_id)
    stmt = (
        select(LedgerEntry)
        .where(LedgerEntry.customer_id == customer_id)
        .order_by(LedgerEntry.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


DUE_FILTERS = ("today", "overdue", "this_week", "all")


async def list_dues(session: AsyncSession, merchant_id: uuid.UUID, filter: str, today: date) -> list[dict]:
    """Customers with outstanding dues matching the filter, biggest first."""
    if filter not in DUE_FILTERS:
        raise LedgerError.of("err_bad_filter", filter=filter)
    stmt = select(Customer).where(Customer.merchant_id == merchant_id, Customer.balance_paise > 0)
    rows = []
    for c in (await session.execute(stmt)).scalars():
        credits = open_credits(await customer_entries(session, c.id))
        overdue = overdue_amount(credits, today)
        due_today = sum(x.remaining_paise for x in credits if x.entry.due_date == today)
        week_end = today + timedelta(days=7)
        due_week = sum(x.remaining_paise for x in credits if x.entry.due_date and today <= x.entry.due_date <= week_end)
        due_date, _ = next_due(credits)
        wanted = {
            "today": due_today + overdue,
            "overdue": overdue,
            "this_week": due_week + overdue,
            "all": c.balance_paise,
        }[filter]
        if wanted > 0:
            rows.append({
                "customer": c,
                "balance_paise": c.balance_paise,
                "due_paise": wanted,
                "overdue_paise": overdue,
                "next_due_date": due_date,
            })
    rows.sort(key=lambda r: r["due_paise"], reverse=True)
    return rows


async def daily_summary(session: AsyncSession, merchant_id: uuid.UUID, day: date) -> dict:
    tz = get_settings().tz
    # Compare in UTC: SQLite stores timestamps without offset.
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    stmt = select(LedgerEntry).where(
        LedgerEntry.merchant_id == merchant_id, LedgerEntry.created_at >= start, LedgerEntry.created_at < end
    )
    entries = list((await session.execute(stmt)).scalars())
    reversed_ids = {e.reverses_entry_id for e in entries if e.type == EntryType.REVERSAL}
    live = [e for e in entries if e.type != EntryType.REVERSAL and e.id not in reversed_ids]
    credit = sum(e.amount_paise for e in live if e.type == EntryType.CREDIT)
    collected = sum(e.amount_paise for e in live if e.type == EntryType.PAYMENT)
    upi = sum(e.amount_paise for e in live if e.type == EntryType.PAYMENT and e.payment_mode == PaymentMode.UPI)
    outstanding_stmt = select(func.coalesce(func.sum(Customer.balance_paise), 0)).where(
        Customer.merchant_id == merchant_id, Customer.balance_paise > 0
    )
    outstanding = (await session.execute(outstanding_stmt)).scalar_one()
    return {
        "date": day,
        "credit_given_paise": credit,
        "collected_paise": collected,
        "collected_upi_paise": upi,
        "entries": len(live),
        "outstanding_paise": int(outstanding),
    }


async def verify_balances(session: AsyncSession, merchant_id: uuid.UUID | None = None) -> list[dict]:
    """Nightly check: re-derive every cached balance from the ledger and fix drift."""
    stmt = select(Customer)
    if merchant_id:
        stmt = stmt.where(Customer.merchant_id == merchant_id)
    fixed = []
    for c in (await session.execute(stmt)).scalars():
        derived = derive_balance(await customer_entries(session, c.id))
        if derived != c.balance_paise:
            fixed.append({"customer_id": str(c.id), "cached": c.balance_paise, "derived": derived})
            c.balance_paise = derived
    await session.flush()
    return fixed
