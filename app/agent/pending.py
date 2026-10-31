"""The confirmation gate: commits or cancels PendingActions. No LLM in this path."""

import uuid
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import NEW_PREFIX, ToolContext, active_pending, customer_card, entry_card
from app.db.models import EntrySource, LedgerEntry, PaymentMode, PendingAction, PendingStatus
from app.i18n import t
from app.ledger import service as ledger
from app.ledger.errors import LedgerError
from app.nlp.hinglish_numbers import format_rupees
from app.payments import reconciler
from app.scheduler.reminders import send_reminders
from app.speech import balance_phrase


@dataclass
class ConfirmOutcome:
    reply: str
    committed: list[dict] = field(default_factory=list)
    outcome: str = "committed"  # committed | awaiting_second_confirm | cancelled | failed


def _customer_id(raw: str, id_map: dict[str, uuid.UUID]) -> uuid.UUID:
    if raw.startswith(NEW_PREFIX):
        if raw not in id_map:
            raise LedgerError.of("err_new_customer_failed")
        return id_map[raw]
    return uuid.UUID(raw)


async def _commit_one(ctx: ToolContext, pa: PendingAction, id_map: dict[str, uuid.UUID]) -> tuple[str, dict]:
    s, m, a = ctx.session, ctx.merchant, pa.args
    lang = m.language
    vl = pa.voice_log_id
    if pa.tool == "create_customer":
        c = await ledger.create_customer(s, m.id, a["name"], a.get("phone"), [a["nickname"]] if a.get("nickname") else [])
        id_map[f"{NEW_PREFIX}{pa.id}"] = c.id
        return t(lang, "did_open_khata", name=c.name), {"customer": customer_card(c)}

    if pa.tool == "add_credit":
        cid = _customer_id(a["customer_id"], id_map)
        due = date.fromisoformat(a["due_date"]) if a.get("due_date") else None
        e = await ledger.add_credit(s, m.id, cid, a["amount_paise"], due, a.get("items"), a.get("note"),
                                    EntrySource.VOICE, vl, a.get("spoken_name"))
        c = await ledger.get_customer(s, m.id, cid)
        return f"{c.name}: {balance_phrase(c.balance_paise, lang)}", {"entry": entry_card(e, c.name), "customer": customer_card(c)}

    if pa.tool == "record_payment":
        cid = _customer_id(a["customer_id"], id_map)
        mode = PaymentMode(a.get("mode", "cash").upper())
        e = await ledger.record_payment(s, m.id, cid, a["amount_paise"], mode, EntrySource.VOICE,
                                        voice_log_id=vl, spoken_name=a.get("spoken_name"))
        c = await ledger.get_customer(s, m.id, cid)
        return f"{c.name}: {balance_phrase(c.balance_paise, lang)}", {"entry": entry_card(e, c.name), "customer": customer_card(c)}

    if pa.tool == "link_upi":
        cid = _customer_id(a["customer_id"], id_map)
        h = await ledger.link_upi(s, m.id, cid, a["vpa"], linked_via="voice")
        c = await ledger.get_customer(s, m.id, cid)
        return t(lang, "did_link_upi", name=c.name), {"vpa": h.vpa, "customer": customer_card(c)}

    if pa.tool == "send_reminder":
        if a.get("customer_id"):
            c = await ledger.get_customer(s, m.id, _customer_id(a["customer_id"], id_map))
            b = await ledger.get_balance(s, m.id, c.id, ctx.today)
            targets = [(c, c.balance_paise, b["next_due_date"])]
        else:
            rows = await ledger.list_dues(s, m.id, "today", ctx.today)
            targets = [(r["customer"], r["balance_paise"], r["next_due_date"]) for r in rows]
        sent = await send_reminders(s, m, targets)
        return t(lang, "did_send_reminders", n=len(sent)), {"reminders": [str(r.id) for r in sent]}

    if pa.tool == "undo_last":
        entry_id = uuid.UUID(a["entry_id"])
        original = await s.get(LedgerEntry, entry_id)
        if original and original.source == EntrySource.UPI_AUTO and original.upi_txn_id:
            await reconciler.undo_auto_payment(s, m.id, original.upi_txn_id)
        else:
            await ledger.reverse_entry(s, m.id, entry_id, note="undo by merchant", voice_log_id=vl)
        c = await ledger.get_customer(s, m.id, original.customer_id)
        return t(lang, "did_undo", name=c.name, balance=balance_phrase(c.balance_paise, lang)), {"customer": customer_card(c)}

    if pa.tool == "assign_upi_payment":
        cid = _customer_id(a["customer_id"], id_map)
        txn, c = await reconciler.assign_payment(s, m.id, a["txn_id"], cid, EntrySource.VOICE, vl)
        return (t(lang, "upi_assigned", amount=format_rupees(txn.amount_paise), name=c.name,
                  balance=balance_phrase(c.balance_paise, lang)),
                {"customer": customer_card(c)})

    raise LedgerError(f"Unknown pending tool {pa.tool}")


async def confirm(ctx: ToolContext) -> ConfirmOutcome | None:
    """Merchant said yes. Commit every pending action of this session atomically."""
    lang = ctx.merchant.language
    actions = await active_pending(ctx.session, ctx.merchant.id, ctx.session_id)
    if not actions:
        return None
    for pa in actions:
        pa.confirmations_received += 1
    waiting = [pa for pa in actions if pa.confirmations_received < pa.confirmations_needed]
    if waiting:
        return ConfirmOutcome(
            reply=t(lang, "big_confirm", summary=waiting[0].summary),
            outcome="awaiting_second_confirm",
        )

    id_map: dict[str, uuid.UUID] = {}
    phrases, committed = [], []
    try:
        async with ctx.session.begin_nested():
            for pa in actions:
                phrase, result = await _commit_one(ctx, pa, id_map)
                pa.status = PendingStatus.COMMITTED
                pa.result = result
                phrases.append(phrase)
                committed.append({"action_id": str(pa.id), "tool": pa.tool, "summary": pa.summary, **result})
    except LedgerError as exc:
        for pa in actions:
            pa.status = PendingStatus.CANCELLED
        return ConfirmOutcome(reply=t(lang, "save_failed", error=exc.text(lang)), outcome="failed")
    return ConfirmOutcome(reply=t(lang, "done") + " " + ". ".join(phrases) + ".", committed=committed)


async def cancel(session: AsyncSession, merchant_id: uuid.UUID, session_id: str) -> int:
    actions = await active_pending(session, merchant_id, session_id)
    for pa in actions:
        pa.status = PendingStatus.CANCELLED
    return len(actions)
