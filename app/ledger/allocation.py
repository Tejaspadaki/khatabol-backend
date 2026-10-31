"""Balance derivation and payment allocation (plan.md §20).

`payment_allocations` records which payment (or waiver) paid which udhaar. It is append-only: a
reversal adds negative rows, never edits old ones, so the net of a (payment, credit) pair is the sum
of its rows. Default order is FIFO by **oldest due date** (udhaar without a due date last, then by
age); a payment can name its udhaar first (targeted).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from app.db.models import EntryType, LedgerEntry, as_utc

REDUCING = (EntryType.PAYMENT, EntryType.ADJUSTMENT)  # entries that pay down udhaar


@dataclass
class OpenCredit:
    entry: LedgerEntry
    remaining_paise: int


def signed_delta(entry: LedgerEntry, by_id: dict[uuid.UUID, LedgerEntry]) -> int:
    """Effect of one entry on the balance. Positive = customer owes more."""
    if entry.type == EntryType.CREDIT:
        return entry.amount_paise
    if entry.type in REDUCING:
        return -entry.amount_paise
    original = by_id.get(entry.reverses_entry_id) if entry.reverses_entry_id else None
    if original is None:
        return 0
    return -signed_delta(original, by_id)


def derive_balance(entries: list[LedgerEntry]) -> int:
    by_id = {e.id: e for e in entries}
    return sum(signed_delta(e, by_id) for e in entries)


def reversed_ids(entries: list[LedgerEntry]) -> set[uuid.UUID]:
    return {e.reverses_entry_id for e in entries if e.type == EntryType.REVERSAL}


def effective_entries(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Drop reversed entries and the reversals themselves; they cancel out."""
    gone = reversed_ids(entries)
    return [e for e in entries if e.type != EntryType.REVERSAL and e.id not in gone]


def due_key(e: LedgerEntry):
    """Oldest due first; udhaar without a due date after all dated ones; then oldest given."""
    return (e.due_date is None, e.due_date or date.max, as_utc(e.created_at))


def net_by(allocations, side: str) -> dict[uuid.UUID, int]:
    """Net allocated amount per credit (side='credit') or per payment (side='payment')."""
    out: dict[uuid.UUID, int] = defaultdict(int)
    for a in allocations:
        out[a.credit_entry_id if side == "credit" else a.payment_entry_id] += a.amount_paise
    return out


def net_pairs(allocations) -> dict[tuple[uuid.UUID, uuid.UUID], int]:
    out: dict[tuple[uuid.UUID, uuid.UUID], int] = defaultdict(int)
    for a in allocations:
        out[(a.payment_entry_id, a.credit_entry_id)] += a.amount_paise
    return {k: v for k, v in out.items() if v}


def open_credits(entries: list[LedgerEntry], allocations) -> list[OpenCredit]:
    """Udhaar not yet fully paid, in allocation order (oldest due first)."""
    paid = net_by(allocations, "credit")
    live = [e for e in effective_entries(entries) if e.type == EntryType.CREDIT]
    result = [OpenCredit(e, e.amount_paise - paid.get(e.id, 0)) for e in sorted(live, key=due_key)]
    return [c for c in result if c.remaining_paise > 0]


def unallocated(entries: list[LedgerEntry], allocations) -> list[tuple[LedgerEntry, int]]:
    """Live payments / waivers with money not yet applied to any udhaar (the advance), oldest first."""
    used = net_by(allocations, "payment")
    live = [e for e in effective_entries(entries) if e.type in REDUCING]
    rows = [(e, e.amount_paise - used.get(e.id, 0)) for e in sorted(live, key=lambda e: as_utc(e.created_at))]
    return [(e, left) for e, left in rows if left > 0]


def entry_statuses(entries: list[LedgerEntry], allocations) -> dict[uuid.UUID, tuple[str, int]]:
    """Per udhaar: OPEN (nothing paid), PARTIAL, PAID, or REVERSED; with the remaining paise."""
    paid = net_by(allocations, "credit")
    gone = reversed_ids(entries)
    out = {}
    for e in entries:
        if e.type != EntryType.CREDIT:
            continue
        left = e.amount_paise - paid.get(e.id, 0)
        if e.id in gone:
            out[e.id] = ("REVERSED", 0)
        else:
            out[e.id] = ("PAID", 0) if left <= 0 else ("OPEN", left) if left == e.amount_paise else ("PARTIAL", left)
    return out


def next_due(credits: list[OpenCredit]) -> tuple[date | None, int]:
    """(earliest due date among open credits, amount due on that date)."""
    dated = [c for c in credits if c.entry.due_date]
    if not dated:
        return None, 0
    d = min(c.entry.due_date for c in dated)
    return d, sum(c.remaining_paise for c in dated if c.entry.due_date == d)


def overdue_amount(credits: list[OpenCredit], today: date) -> int:
    return sum(c.remaining_paise for c in credits if c.entry.due_date and c.entry.due_date < today)
