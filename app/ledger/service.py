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
    CustomerEvent,
    CustomerUpiHandle,
    EntrySource,
    Family,
    EntryType,
    LedgerEntry,
    Merchant,
    PaymentAllocation,
    PaymentMode,
    as_utc,
)
from app.i18n import t
from app.ledger.allocation import (
    derive_balance,
    effective_entries,
    entry_statuses as _entry_statuses,
    net_pairs,
    next_due,
    open_credits,
    overdue_amount,
    signed_delta,
    unallocated,
)
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.hinglish_numbers import format_rupees
from app.notify.service import account_for_phone, note_change
from app.nlp.names import CandidateName, decide, normalize_name, phonetic_key, score_candidates

MAX_AMOUNT_PAISE = 10_000_000_00  # ₹1 crore sanity cap
NEAR_LIMIT_PCT = 80  # the udhaar card warns at this share of the credit limit


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
    acc = await account_for_phone(session, phone, name)  # one person across shops (§18.3)
    c.customer_account_id = acc.id if acc else None
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


# ---------------------------------------------------------------- credit limits and customer events

async def record_event(
    session: AsyncSession, customer: Customer, event_type: str, payload: dict, actor: str = "merchant"
) -> CustomerEvent:
    ev = CustomerEvent(customer_id=customer.id, merchant_id=customer.merchant_id, event_type=event_type,
                       payload=payload, actor=actor)
    session.add(ev)
    await session.flush()
    return ev


async def set_credit_limit(
    session: AsyncSession, merchant_id: uuid.UUID, customer_id: uuid.UUID, limit_paise: int | None,
    actor: str = "merchant",
) -> CustomerEvent:
    """None removes the limit; 0 blocks all new udhaar."""
    if limit_paise is not None:
        if limit_paise < 0:
            raise LedgerError.of("err_limit_negative")
        if limit_paise > MAX_AMOUNT_PAISE:
            raise LedgerError.of("err_amount_too_big")
    customer = await get_customer(session, merchant_id, customer_id, lock=True)
    old = customer.credit_limit_paise
    customer.credit_limit_paise = limit_paise
    return await record_event(session, customer, "CREDIT_LIMIT_SET",
                              {"old_limit_paise": old, "new_limit_paise": limit_paise}, actor)


def credit_status(customer: Customer, extra_paise: int = 0) -> dict:
    """Where the balance stands against the limit, optionally after `extra_paise` more udhaar."""
    limit = customer.credit_limit_paise
    balance = customer.balance_paise or 0
    projected = balance + extra_paise
    status = {"balance_paise": balance, "limit_paise": limit, "projected_paise": projected,
              "used_pct": None, "over_by_paise": 0, "near_limit": False}
    if limit is None:
        return status
    status["over_by_paise"] = max(0, projected - limit)
    if limit > 0:
        status["used_pct"] = max(0, projected) * 100 // limit
        status["near_limit"] = status["over_by_paise"] == 0 and projected * 100 >= NEAR_LIMIT_PCT * limit
    return status


def _check_limit(customer: Customer, amount_paise: int, allow_over_limit_paise: int) -> int:
    """Raise unless this udhaar fits the limit (or the confirmed overage). Returns the overage."""
    limit = customer.credit_limit_paise
    if limit is None:
        return 0
    if limit == 0:
        raise LedgerError.of("err_limit_zero", name=customer.name)
    over = credit_status(customer, amount_paise)["over_by_paise"]
    if over > max(0, allow_over_limit_paise):
        raise LedgerError.of("err_over_limit", name=customer.name, limit=format_rupees(limit),
                             over=format_rupees(over))
    return over


# ---------------------------------------------------------------- holds

def overdue_beyond_grace(credits: list, today: date, grace_days: int) -> list:
    """Open credits whose due date is more than `grace_days` in the past. No due date = never."""
    return [x for x in credits if x.entry.due_date and (today - x.entry.due_date).days > grace_days]


def hold_refusal(customer: Customer) -> LedgerError:
    reason = f" ({customer.hold_reason})" if customer.hold_reason else ""
    return LedgerError.of("err_on_hold", name=customer.name, reason=reason)


async def set_hold(
    session: AsyncSession, merchant_id: uuid.UUID, customer_id: uuid.UUID, on_hold: bool, reason: str | None,
    actor: str, today: date,
) -> CustomerEvent:
    """Place or lift a hold. The event keeps the reason, the actor and the overdue amount at that moment.

    A lift also records which entries were past the grace period, so the daily job does not re-hold
    for those (BUILD_CONTEXT.md §11.2); only an entry that goes overdue later puts the hold back.
    """
    customer = await get_customer(session, merchant_id, customer_id, lock=True)
    if on_hold and customer.on_hold:
        raise LedgerError.of("err_already_on_hold", name=customer.name)
    if not on_hold and not customer.on_hold:
        raise LedgerError.of("err_not_on_hold", name=customer.name)
    reason = " ".join((reason or "").split()) or None
    credits = await open_credits_for(session, customer.id)
    payload = {"reason": reason, "overdue_paise": overdue_amount(credits, today)}
    if not on_hold:
        merchant = await get_merchant(session, merchant_id)
        payload["overdue_entry_ids"] = [
            str(x.entry.id) for x in overdue_beyond_grace(credits, today, merchant.hold_grace_days)]
    customer.on_hold = on_hold
    customer.hold_reason = reason if on_hold else None
    return await record_event(session, customer, "HOLD_PLACED" if on_hold else "HOLD_LIFTED", payload, actor)


async def _last_hold_event(session: AsyncSession, customer_id: uuid.UUID) -> CustomerEvent | None:
    stmt = (
        select(CustomerEvent)
        .where(CustomerEvent.customer_id == customer_id,
               CustomerEvent.event_type.in_(["HOLD_PLACED", "HOLD_LIFTED"]))
        .order_by(CustomerEvent.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def apply_overdue_holds(session: AsyncSession, today: date, merchant_id: uuid.UUID | None = None) -> list[Customer]:
    """Daily job: hold every customer with udhaar overdue beyond the shop's grace days. Caller commits."""
    stmt = select(Customer).where(Customer.balance_paise > 0, Customer.on_hold.is_(False)).order_by(Customer.created_at)
    if merchant_id:
        stmt = stmt.where(Customer.merchant_id == merchant_id)
    merchants: dict[uuid.UUID, Merchant] = {}
    held = []
    for c in list((await session.execute(stmt)).scalars()):
        m = merchants.get(c.merchant_id) or await get_merchant(session, c.merchant_id)
        merchants[m.id] = m
        beyond = overdue_beyond_grace(await open_credits_for(session, c.id), today, m.hold_grace_days)
        last = await _last_hold_event(session, c.id) if beyond else None
        if last and last.event_type == "HOLD_LIFTED" and last.actor == "merchant":
            excused = set(last.payload.get("overdue_entry_ids") or [])
            beyond = [x for x in beyond if str(x.entry.id) not in excused]
        if not beyond:
            continue
        oldest = min(x.entry.due_date for x in beyond)
        reason = t(m.language, "hold_reason_overdue", amount=format_rupees(sum(x.remaining_paise for x in beyond)),
                   days=(today - oldest).days)
        await set_hold(session, m.id, c.id, True, reason, "system", today)
        held.append(c)
    return held


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
    note_change(session, "entry", entry.id)  # the customer is notified of every change (§18)
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
    allow_over_limit_paise: int = 0,
    allow_on_hold: bool = False,
    taken_by_customer_id: uuid.UUID | None = None,
    allow_no_permission: bool = False,
    allow_member_over_paise: int = 0,
    allow_family_over_paise: int = 0,
    family_level: bool = False,
) -> LedgerEntry:
    """allow_over_limit_paise: the overage the merchant explicitly confirmed (0 = stay within the limit).
    allow_on_hold: only for the corrected copy of an edited entry (§19), which is not new udhaar.

    Family (§17): udhaar by a family member is posted on the head's khata (family_id, taken_by). The member's
    permission, member limit and family limit are checked here; each can only be passed by the explicit
    "haan, phir bhi" the merchant confirmed (allow_*)."""
    _check_amount(amount_paise)
    customer = await get_customer(session, merchant_id, customer_id, lock=True)
    account, family, member = await khata_route(session, merchant_id, customer, taken_by_customer_id)
    if family_level:
        member = None  # "Sharma parivar mein 300 likho": nobody named
    if account.on_hold and not allow_on_hold:
        raise hold_refusal(account)
    family_over = await _check_family(session, account, family, member, amount_paise,
                                      allow_no_permission or allow_on_hold, allow_member_over_paise,
                                      allow_family_over_paise)
    over_by = _check_limit(account, amount_paise, allow_over_limit_paise)
    learn_nickname(customer, spoken_name)
    entry = LedgerEntry(
        merchant_id=merchant_id,
        customer_id=account.id,
        family_id=family.id if family else None,
        taken_by_customer_id=member.id if member else None,
        type=EntryType.CREDIT,
        amount_paise=amount_paise,
        due_date=due_date,
        items=items,
        note=note,
        source=source,
        voice_log_id=voice_log_id,
        spoken_name=spoken_name,
    )
    await _append(session, account, entry, amount_paise)
    await _rebalance(session, account)  # an advance pays this udhaar straight away
    if over_by:
        await record_event(session, account, "CREDIT_LIMIT_OVERRIDE", {
            "entry_id": str(entry.id), "limit_paise": account.credit_limit_paise, "over_by_paise": over_by})
    if family_over:
        await record_event(session, account, "FAMILY_OVERRIDE", {"entry_id": str(entry.id), **family_over})
    return entry


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
    target_entry_id: uuid.UUID | None = None,
) -> LedgerEntry:
    """target_entry_id: the udhaar the merchant named ("Diwali wale bill ka 300"); paid first, rest oldest-due."""
    _check_amount(amount_paise)
    named = await get_customer(session, merchant_id, customer_id, lock=True)
    customer, family, member = await khata_route(session, merchant_id, named)  # a member pays the family khata
    target = await _check_target(session, customer, target_entry_id)
    learn_nickname(named, spoken_name)
    entry = LedgerEntry(
        merchant_id=merchant_id,
        customer_id=customer.id,
        family_id=family.id if family else None,
        taken_by_customer_id=member.id if member else None,
        type=EntryType.PAYMENT,
        amount_paise=amount_paise,
        payment_mode=mode,
        note=note,
        source=source,
        upi_txn_id=upi_txn_id,
        voice_log_id=voice_log_id,
        spoken_name=spoken_name,
    )
    await _append(session, customer, entry, -amount_paise)
    await _apply(session, customer, entry, amount_paise, target)
    return entry


async def record_adjustment(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    customer_id: uuid.UUID,
    amount_paise: int,
    reason: str,
    target_entry_id: uuid.UUID | None = None,
    source: EntrySource = EntrySource.VOICE,
    voice_log_id: uuid.UUID | None = None,
) -> LedgerEntry:
    """Waiver or discount ("Gupta ji ke 50 chhod do"): lowers what is owed, is not money received."""
    _check_amount(amount_paise)
    reason = " ".join((reason or "").split())
    if not reason:
        raise LedgerError.of("err_reason_needed")
    customer, family, _ = await khata_route(session, merchant_id,
                                            await get_customer(session, merchant_id, customer_id, lock=True))
    if amount_paise > max(customer.balance_paise or 0, 0):
        raise LedgerError.of("err_waive_too_much", name=customer.name, amount=format_rupees(max(customer.balance_paise, 0)))
    target = await _check_target(session, customer, target_entry_id)
    entry = LedgerEntry(merchant_id=merchant_id, customer_id=customer.id, family_id=family.id if family else None,
                        type=EntryType.ADJUSTMENT,
                        amount_paise=amount_paise, reason=reason, note=reason, source=source, voice_log_id=voice_log_id)
    await _append(session, customer, entry, -amount_paise)
    await _apply(session, customer, entry, amount_paise, target)
    return entry


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
    await _append(session, customer, entry, -signed_delta(original, {}))
    # Its allocations are cancelled with negative rows (never edited); then any freed money or
    # reopened udhaar is matched up again.
    for (pay_id, credit_id), net in net_pairs(await customer_allocations(session, customer.id)).items():
        if original.id in (pay_id, credit_id):
            session.add(PaymentAllocation(merchant_id=merchant_id, customer_id=customer.id, payment_entry_id=pay_id,
                                          credit_entry_id=credit_id, amount_paise=-net, reversal_entry_id=entry.id))
    await session.flush()
    await _rebalance(session, customer)
    return entry


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


# ---------------------------------------------------------------- family khata routing (plan.md §17)

async def khata_route(session: AsyncSession, merchant_id: uuid.UUID, customer: Customer,
                      taken_by_customer_id: uuid.UUID | None = None):
    """(account that owes, family or None, member or None). A family member's entries go on the head's khata."""
    if customer.family_id is None:
        return customer, None, None
    family = await session.get(Family, customer.family_id)
    head = await get_customer(session, merchant_id, family.head_customer_id, lock=True)
    member = customer
    if taken_by_customer_id and taken_by_customer_id != customer.id:
        member = await get_customer(session, merchant_id, taken_by_customer_id)
        if member.family_id != family.id:
            member = None  # e.g. the corrected copy of an entry whose member has since left: family-level
    return head, family, member


async def khata_account(session: AsyncSession, customer: Customer) -> Customer:
    return (await khata_route(session, customer.merchant_id, customer))[0]


async def member_outstanding(session: AsyncSession, account_id: uuid.UUID, member_id: uuid.UUID) -> int:
    return sum(c.remaining_paise for c in await open_credits_for(session, account_id)
               if c.entry.taken_by_customer_id == member_id)


async def _check_family(session: AsyncSession, account: Customer, family, member, amount: int, allow_no_permission: bool,
                        allow_member_over: int, allow_family_over: int) -> dict:
    """Member permission, member limit, family limit (§17.4). Returns the overrides that were used."""
    if family is None:
        return {}
    used = {}
    if member is not None and not member.can_take_credit:
        if not allow_no_permission:
            raise LedgerError.of("err_member_no_permission", name=member.name)
        used["no_permission"] = member.name
    if member is not None and member.member_limit_paise is not None:
        over = await member_outstanding(session, account.id, member.id) + amount - member.member_limit_paise
        if over > 0:
            if over > allow_member_over:
                raise LedgerError.of("err_member_over_limit", name=member.name,
                                     limit=format_rupees(member.member_limit_paise), over=format_rupees(over))
            used["member_over_paise"] = over
    if family.credit_limit_paise is not None:
        over = (account.balance_paise or 0) + amount - family.credit_limit_paise
        if over > 0:
            if over > allow_family_over:
                raise LedgerError.of("err_family_over_limit", name=family.display_name,
                                     limit=format_rupees(family.credit_limit_paise), over=format_rupees(over))
            used["family_over_paise"] = over
    return used


# ---------------------------------------------------------------- allocations (plan.md §20)

async def customer_allocations(session: AsyncSession, customer_id: uuid.UUID) -> list[PaymentAllocation]:
    stmt = select(PaymentAllocation).where(PaymentAllocation.customer_id == customer_id)
    return list((await session.execute(stmt)).scalars())


async def credit_book(session: AsyncSession, customer_id: uuid.UUID):
    return await customer_entries(session, customer_id), await customer_allocations(session, customer_id)


async def open_credits_for(session: AsyncSession, customer_id: uuid.UUID):
    return open_credits(*await credit_book(session, customer_id))


async def entry_statuses(session: AsyncSession, customer_id: uuid.UUID) -> dict[uuid.UUID, tuple[str, int]]:
    """OPEN / PARTIAL / PAID / REVERSED and the remaining paise for each udhaar of the customer."""
    return _entry_statuses(*await credit_book(session, customer_id))


async def _check_target(session: AsyncSession, customer: Customer, target_entry_id: uuid.UUID | None):
    if target_entry_id is None:
        return None
    match = [c for c in await open_credits_for(session, customer.id) if c.entry.id == target_entry_id]
    if not match:
        raise LedgerError.of("err_target_not_open", name=customer.name)
    return match[0]


async def _apply(session: AsyncSession, customer: Customer, entry: LedgerEntry, amount: int, target) -> None:
    """Allocate a new payment / waiver: the named udhaar first, then oldest due first; the rest is advance."""
    if target is not None:
        used = min(amount, target.remaining_paise)
        session.add(PaymentAllocation(merchant_id=customer.merchant_id, customer_id=customer.id,
                                      payment_entry_id=entry.id, credit_entry_id=target.entry.id, amount_paise=used))
        await session.flush()
    await _rebalance(session, customer)


async def _rebalance(session: AsyncSession, customer: Customer) -> None:
    """Match unapplied payment money (oldest first) with open udhaar (oldest due first)."""
    entries, allocs = await credit_book(session, customer.id)
    credits = open_credits(entries, allocs)
    for pay, left in unallocated(entries, allocs):
        while left and credits:
            c = credits[0]
            used = min(left, c.remaining_paise)
            session.add(PaymentAllocation(merchant_id=customer.merchant_id, customer_id=customer.id,
                                          payment_entry_id=pay.id, credit_entry_id=c.entry.id, amount_paise=used))
            left -= used
            c.remaining_paise -= used
            if not c.remaining_paise:
                credits.pop(0)
    await session.flush()


# ---------------------------------------------------------------- reads

async def customer_entries(session: AsyncSession, customer_id: uuid.UUID) -> list[LedgerEntry]:
    stmt = select(LedgerEntry).where(LedgerEntry.customer_id == customer_id).order_by(LedgerEntry.created_at)
    return list((await session.execute(stmt)).scalars())


async def get_balance(session: AsyncSession, merchant_id: uuid.UUID, customer_id: uuid.UUID, today: date) -> dict:
    customer = await get_customer(session, merchant_id, customer_id)
    entries, allocs = await credit_book(session, customer_id)
    credits = open_credits(entries, allocs)
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
        credits = await open_credits_for(session, c.id)
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
    waived = sum(e.amount_paise for e in live if e.type == EntryType.ADJUSTMENT)
    outstanding_stmt = select(func.coalesce(func.sum(Customer.balance_paise), 0)).where(
        Customer.merchant_id == merchant_id, Customer.balance_paise > 0
    )
    outstanding = (await session.execute(outstanding_stmt)).scalar_one()
    return {
        "date": day,
        "credit_given_paise": credit,
        "collected_paise": collected,
        "collected_upi_paise": upi,
        "waived_paise": waived,
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
