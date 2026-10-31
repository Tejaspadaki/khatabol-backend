"""Edit and update records (plan.md §19). The ledger stays append-only: an edit is a reversal plus a
corrected entry, and every changed field is a row in `entry_edits` (also append-only)."""

import uuid
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import EntryEdit, EntryType, LedgerEntry, PaymentMode, Staff, as_utc
from app.ledger import service as ledger
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.hinglish_numbers import format_rupees
from app.notify import disputes
from app.notify.service import note_change

EDITABLE = (EntryType.CREDIT, EntryType.PAYMENT, EntryType.ADJUSTMENT)
OLD_ENTRY_DAYS = 7  # editing anything older needs a second confirmation (§19.3)
_UNSET = object()


# ---------------------------------------------------------------- who is acting

async def add_staff(session: AsyncSession, merchant_id: uuid.UUID, name: str, role: str = "staff") -> Staff:
    if role not in ("owner", "staff"):
        raise LedgerError("role must be owner or staff.")
    s = Staff(merchant_id=merchant_id, name=" ".join(name.split()), role=role)
    session.add(s)
    await session.flush()
    return s


async def list_staff(session: AsyncSession, merchant_id: uuid.UUID) -> list[Staff]:
    return list((await session.execute(select(Staff).where(Staff.merchant_id == merchant_id))).scalars())


async def actor_for(session: AsyncSession, merchant_id: uuid.UUID, staff_id: uuid.UUID | None) -> str:
    """'owner' for the shop's own device (no staff id) or an owner-role staff member; else 'staff:<id>'."""
    if staff_id is None:
        return "owner"
    s = await session.get(Staff, staff_id)
    if s is None or s.merchant_id != merchant_id:
        raise NotFoundError.of("err_staff_not_found")
    return "owner" if s.role == "owner" else f"staff:{s.id}"


# ---------------------------------------------------------------- edits

async def live_entry(session: AsyncSession, merchant_id: uuid.UUID, entry_id: uuid.UUID) -> LedgerEntry:
    e = await session.get(LedgerEntry, entry_id)
    if e is None or e.merchant_id != merchant_id:
        raise NotFoundError.of("err_entry_not_found")
    if e.type not in EDITABLE:
        raise LedgerError.of("err_not_editable")
    if await ledger._is_reversed(session, e.id):
        raise LedgerError.of("err_entry_cancelled")
    return e


def _show(field: str, value) -> str | None:
    if value is None:
        return None
    if field == "amount":
        return format_rupees(value)
    return value.isoformat() if isinstance(value, date) else str(value)


async def edit_entry(
    session: AsyncSession, merchant_id: uuid.UUID, entry_id: uuid.UUID, *, amount_paise: int | None = None,
    due_date=_UNSET, items=_UNSET, to_customer_id: uuid.UUID | None = None, reason: str, edited_by: str,
    approved_by: str | None = None, voice_log_id: uuid.UUID | None = None,
) -> tuple[LedgerEntry, LedgerEntry, list[EntryEdit]]:
    """Reversal of the original + a corrected copy. The original row is never touched. A correction is not
    new udhaar, so a hold doesn't block it; the credit limit still applies to the corrected amount."""
    reason = " ".join((reason or "").split())
    if not reason:
        raise LedgerError.of("err_reason_needed")
    original = await live_entry(session, merchant_id, entry_id)
    old_customer = await ledger.get_customer(session, merchant_id, original.customer_id)
    new_customer = await ledger.get_customer(session, merchant_id, to_customer_id) if to_customer_id else old_customer
    amount = amount_paise if amount_paise is not None else original.amount_paise
    due = original.due_date if due_date is _UNSET else due_date
    items_ = original.items if items is _UNSET else items
    changes = []
    if amount != original.amount_paise:
        changes.append(("amount", original.amount_paise, amount))
    if original.type == EntryType.CREDIT and due != original.due_date:
        changes.append(("due_date", original.due_date, due))
    if original.type == EntryType.CREDIT and items_ != original.items:
        changes.append(("items", original.items, items_))
    if new_customer.id != old_customer.id:
        changes.append(("customer", old_customer.name, new_customer.name))
    if not changes:
        raise LedgerError.of("err_nothing_to_change")

    async with session.begin_nested():  # all or nothing: e.g. an over-limit correction undoes its reversal
        reversal = await ledger.reverse_entry(session, merchant_id, original.id, note=f"edit: {reason}",
                                              source=original.source, voice_log_id=voice_log_id)
        if original.type == EntryType.CREDIT:
            new = await ledger.add_credit(session, merchant_id, new_customer.id, amount, due, items_, original.note,
                                          original.source, voice_log_id, allow_on_hold=True,
                                          taken_by_customer_id=original.taken_by_customer_id
                                          if new_customer.id == old_customer.id else None)
        elif original.type == EntryType.PAYMENT:
            new = await ledger.record_payment(session, merchant_id, new_customer.id, amount,
                                              original.payment_mode or PaymentMode.CASH, original.source,
                                              note=original.note, voice_log_id=voice_log_id)
        else:
            new = await ledger.record_adjustment(session, merchant_id, new_customer.id, amount,
                                                 original.reason or reason, source=original.source,
                                                 voice_log_id=voice_log_id)
        rows = [EntryEdit(merchant_id=merchant_id, customer_id=old_customer.id, original_entry_id=original.id,
                          reversal_entry_id=reversal.id, new_entry_id=new.id, field_changed=f,
                          old_value=_show(f, old), new_value=_show(f, value), reason=reason, edited_by=edited_by,
                          approved_by=approved_by)
                for f, old, value in changes]
        session.add_all(rows)
        await session.flush()
        note_change(session, "edit", original.id, reversal.id, new.id,
                     [(f, _show(f, old), _show(f, value)) for f, old, value in changes])
        await disputes.resolve_by_edit(session, original.id)
    return reversal, new, rows


async def cancel_entry(session: AsyncSession, merchant_id: uuid.UUID, entry_id: uuid.UUID, reason: str,
                       edited_by: str, approved_by: str | None = None, voice_log_id=None) -> LedgerEntry:
    original = await live_entry(session, merchant_id, entry_id)
    reversal = await ledger.reverse_entry(session, merchant_id, original.id, note=f"cancel: {reason}",
                                          source=original.source, voice_log_id=voice_log_id)
    session.add(EntryEdit(merchant_id=merchant_id, customer_id=original.customer_id, original_entry_id=original.id,
                          reversal_entry_id=reversal.id, field_changed="cancel",
                          old_value=format_rupees(original.amount_paise), reason=reason or "cancel",
                          edited_by=edited_by, approved_by=approved_by))
    await session.flush()
    note_change(session, "cancel", original.id, reversal.id, None)
    await disputes.resolve_by_edit(session, original.id)
    return reversal


async def current_note(session: AsyncSession, entry: LedgerEntry) -> str | None:
    stmt = (select(EntryEdit.new_value)
            .where(EntryEdit.original_entry_id == entry.id, EntryEdit.field_changed == "note")
            .order_by(EntryEdit.created_at.desc()).limit(1))
    latest = (await session.execute(stmt)).scalar_one_or_none()
    return latest if latest is not None else entry.note


async def add_note(session: AsyncSession, merchant_id: uuid.UUID, entry_id: uuid.UUID, note: str, edited_by: str,
                   approved_by: str | None = None) -> EntryEdit:
    """Note history on the entry: no ledger row changes, each note is a new entry_edits row."""
    e = await live_entry(session, merchant_id, entry_id)
    row = EntryEdit(merchant_id=merchant_id, customer_id=e.customer_id, original_entry_id=e.id, field_changed="note",
                    old_value=await current_note(session, e), new_value=" ".join(note.split()), reason="note",
                    edited_by=edited_by, approved_by=approved_by)
    session.add(row)
    await session.flush()
    return row


async def entry_history(session: AsyncSession, merchant_id: uuid.UUID, entry_id: uuid.UUID) -> list[EntryEdit]:
    """Every edit along the chain this entry belongs to (earlier versions and later corrections)."""
    ids, frontier = {entry_id}, {entry_id}
    while frontier:
        stmt = select(EntryEdit).where(EntryEdit.merchant_id == merchant_id, or_(
            EntryEdit.original_entry_id.in_(frontier), EntryEdit.new_entry_id.in_(frontier)))
        rows = list((await session.execute(stmt)).scalars())
        found = {r.original_entry_id for r in rows} | {r.new_entry_id for r in rows if r.new_entry_id}
        frontier = found - ids
        ids |= found
    stmt = select(EntryEdit).where(EntryEdit.merchant_id == merchant_id, EntryEdit.original_entry_id.in_(ids))
    return sorted((await session.execute(stmt)).scalars(), key=lambda r: r.created_at)


async def customer_edits(session: AsyncSession, merchant_id: uuid.UUID, customer_id: uuid.UUID) -> list[EntryEdit]:
    stmt = (select(EntryEdit).where(EntryEdit.merchant_id == merchant_id, EntryEdit.customer_id == customer_id)
            .order_by(EntryEdit.created_at))
    return list((await session.execute(stmt)).scalars())


def edit_card(r: EntryEdit, names: dict[str, str] | None = None) -> dict:
    return {"edit_id": str(r.id), "entry_id": str(r.original_entry_id),
            "new_entry_id": str(r.new_entry_id) if r.new_entry_id else None, "field": r.field_changed,
            "old": r.old_value, "new": r.new_value, "reason": r.reason,
            "by": (names or {}).get(r.edited_by, r.edited_by), "approved_by": r.approved_by,
            "at": r.created_at.isoformat() if r.created_at else None}


async def actor_names(session: AsyncSession, merchant_id: uuid.UUID) -> dict[str, str]:
    return {f"staff:{s.id}": s.name for s in await list_staff(session, merchant_id)}


def is_old(entry: LedgerEntry, today: date) -> bool:
    return (today - as_utc(entry.created_at).astimezone(get_settings().tz).date()).days > OLD_ENTRY_DAYS

