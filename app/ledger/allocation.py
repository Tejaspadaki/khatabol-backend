"""Balance derivation and FIFO allocation of payments against credits (plan.md §5.3)."""

import uuid
from dataclasses import dataclass
from datetime import date

from app.db.models import EntryType, LedgerEntry, as_utc


@dataclass
class OpenCredit:
    entry: LedgerEntry
    remaining_paise: int


def signed_delta(entry: LedgerEntry, by_id: dict[uuid.UUID, LedgerEntry]) -> int:
    """Effect of one entry on the balance. Positive = customer owes more."""
    if entry.type == EntryType.CREDIT:
        return entry.amount_paise
    if entry.type == EntryType.PAYMENT:
        return -entry.amount_paise
    original = by_id.get(entry.reverses_entry_id) if entry.reverses_entry_id else None
    if original is None:
        return 0
    return -signed_delta(original, by_id)


def derive_balance(entries: list[LedgerEntry]) -> int:
    by_id = {e.id: e for e in entries}
    return sum(signed_delta(e, by_id) for e in entries)


def effective_entries(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Drop reversed entries and the reversals themselves; they cancel out."""
    reversed_ids = {e.reverses_entry_id for e in entries if e.type == EntryType.REVERSAL}
    return [e for e in entries if e.type != EntryType.REVERSAL and e.id not in reversed_ids]


def open_credits(entries: list[LedgerEntry]) -> list[OpenCredit]:
    """Credits not yet covered by payments, oldest first. Payments clear the oldest credit first."""
    live = sorted(effective_entries(entries), key=lambda e: as_utc(e.created_at))
    pool = sum(e.amount_paise for e in live if e.type == EntryType.PAYMENT)
    result = []
    for e in live:
        if e.type != EntryType.CREDIT:
            continue
        used = min(pool, e.amount_paise)
        pool -= used
        if e.amount_paise - used > 0:
            result.append(OpenCredit(e, e.amount_paise - used))
    return result


def next_due(credits: list[OpenCredit]) -> tuple[date | None, int]:
    """(earliest due date among open credits, amount due on that date)."""
    dated = [c for c in credits if c.entry.due_date]
    if not dated:
        return None, 0
    d = min(c.entry.due_date for c in dated)
    return d, sum(c.remaining_paise for c in dated if c.entry.due_date == d)


def overdue_amount(credits: list[OpenCredit], today: date) -> int:
    return sum(c.remaining_paise for c in credits if c.entry.due_date and c.entry.due_date < today)
