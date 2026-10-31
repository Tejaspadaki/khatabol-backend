"""The confirmation gate: commits or cancels PendingActions. No LLM in this path."""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import NEW_PREFIX, ToolContext, active_pending, customer_card, entry_card
from app.config import get_settings
from app.db.models import EntrySource, LedgerEntry, PaymentMode, PendingAction, PendingStatus
from app.ledger import edits
from app.i18n import t
from app.ledger import service as ledger
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.hinglish_numbers import format_rupees
from app.payments import reconciler
from app.scheduler.reminders import send_payment_nudge, send_reminders
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
                                    EntrySource.VOICE, vl, a.get("spoken_name"),
                                    allow_over_limit_paise=a.get("over_limit_paise") or 0,
                                    allow_no_permission=bool(a.get("allow_no_permission")),
                                    allow_member_over_paise=a.get("allow_member_over_paise") or 0,
                                    allow_family_over_paise=a.get("allow_family_over_paise") or 0,
                                    family_level=bool(a.get("family_level")))
        c = await ledger.get_customer(s, m.id, cid)
        return f"{c.name}: {balance_phrase(c.balance_paise, lang)}", {"entry": entry_card(e, c.name), "customer": customer_card(c)}

    if pa.tool == "record_payment":
        cid = _customer_id(a["customer_id"], id_map)
        mode = PaymentMode(a.get("mode", "cash").upper())
        e = await ledger.record_payment(s, m.id, cid, a["amount_paise"], mode, EntrySource.VOICE,
                                        voice_log_id=vl, spoken_name=a.get("spoken_name"),
                                        target_entry_id=uuid.UUID(a["target_entry_id"]) if a.get("target_entry_id") else None)
        c = await ledger.get_customer(s, m.id, cid)
        # Read back what was paid and what is still due; offer a nudge for the rest (sent only after a "haan").
        nudge = None
        if c.balance_paise > 0 and m.upi_vpa:
            nudge = {"payment_id": str(e.id), "customer_id": str(c.id), "remaining": format_rupees(c.balance_paise)}
        phrase = t(lang, "did_payment", name=c.name, paid=format_rupees(e.amount_paise),
                   balance=balance_phrase(c.balance_paise, lang))
        return phrase, {"entry": entry_card(e, c.name), "customer": customer_card(c), "nudge": nudge}

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
            approved_by = ctx.actor if a.get("edited_by", ctx.actor) != ctx.actor else None
            await edits.cancel_entry(s, m.id, entry_id, "undo", a.get("edited_by", ctx.actor), approved_by, vl)
        c = await ledger.get_customer(s, m.id, original.customer_id)
        return t(lang, "did_undo", name=c.name, balance=balance_phrase(c.balance_paise, lang)), {"customer": customer_card(c)}

    if pa.tool == "assign_upi_payment":
        cid = _customer_id(a["customer_id"], id_map)
        txn, c = await reconciler.assign_payment(s, m.id, a["txn_id"], cid, EntrySource.VOICE, vl)
        return (t(lang, "upi_assigned", amount=format_rupees(txn.amount_paise), name=c.name,
                  balance=balance_phrase(c.balance_paise, lang)),
                {"customer": customer_card(c)})

    if pa.tool == "set_credit_limit":
        cid = _customer_id(a["customer_id"], id_map)
        await ledger.set_credit_limit(s, m.id, cid, a["limit_paise"], actor="merchant")
        c = await ledger.get_customer(s, m.id, cid)
        return t(lang, "did_set_limit", name=c.name, amount=format_rupees(a["limit_paise"])), {"customer": customer_card(c)}

    if pa.tool == "set_hold":
        cid = _customer_id(a["customer_id"], id_map)
        await ledger.set_hold(s, m.id, cid, a["on_hold"], a.get("reason"), "merchant", ctx.today)
        c = await ledger.get_customer(s, m.id, cid)
        return t(lang, "did_hold" if a["on_hold"] else "did_unhold", name=c.name), {"customer": customer_card(c)}

    if pa.tool == "send_partial_nudge":
        c = await ledger.get_customer(s, m.id, _customer_id(a["customer_id"], id_map))
        r = await send_payment_nudge(s, m, c, uuid.UUID(a["payment_id"]), ctx.today)
        return t(lang, "did_nudge", name=c.name, amount=format_rupees(r.amount_paise)), {"reminders": [str(r.id)]}

    if pa.tool == "edit_entry":
        approved_by = ctx.actor if a["edited_by"] != ctx.actor else None
        eid = uuid.UUID(a["entry_id"])
        original = await s.get(LedgerEntry, eid)
        cid = uuid.UUID(a["to_customer_id"]) if a.get("to_customer_id") else original.customer_id
        if a.get("cancel"):
            await edits.cancel_entry(s, m.id, eid, a["reason"], a["edited_by"], approved_by, vl)
        else:
            kw = {}
            if a.get("amount_paise") is not None:
                kw["amount_paise"] = a["amount_paise"]
            if a.get("set_due"):
                kw["due_date"] = date.fromisoformat(a["due_date"]) if a.get("due_date") else None
            if a.get("set_items"):
                kw["items"] = a.get("items")
            if a.get("to_customer_id"):
                kw["to_customer_id"] = cid
            target = eid
            if kw:
                _, new, _ = await edits.edit_entry(s, m.id, eid, reason=a["reason"], edited_by=a["edited_by"],
                                                   approved_by=approved_by, voice_log_id=vl, **kw)
                target = new.id
            if a.get("note"):
                await edits.add_note(s, m.id, target, a["note"], a["edited_by"], approved_by)
        c = await ledger.get_customer(s, m.id, cid)
        return t(lang, "did_edit", name=c.name, balance=balance_phrase(c.balance_paise, lang)), {"customer": customer_card(c)}

    if pa.tool == "waive_amount":
        cid = _customer_id(a["customer_id"], id_map)
        await ledger.record_adjustment(s, m.id, cid, a["amount_paise"], a["reason"],
                                       uuid.UUID(a["target_entry_id"]) if a.get("target_entry_id") else None,
                                       EntrySource.VOICE, vl)
        c = await ledger.get_customer(s, m.id, cid)
        return (t(lang, "did_waive", name=c.name, amount=format_rupees(a["amount_paise"]),
                  balance=balance_phrase(c.balance_paise, lang)), {"customer": customer_card(c)})

    if pa.tool in ("create_family", "add_member", "remove_member", "merge_families"):
        from app.family import service as family

        if pa.tool == "create_family":
            f = await family.create_family(s, m.id, a["name"], uuid.UUID(a["head_id"]), a.get("area_hint"), a.get("limit_paise"))
            return t(lang, "did_create_family", name=f.display_name), {"family_id": str(f.id)}
        if pa.tool == "add_member":
            c = await family.add_member(s, m.id, uuid.UUID(a["family_id"]), a.get("name"), a.get("relation"), a.get("phone"),
                                        uuid.UUID(a["customer_id"]) if a.get("customer_id") else None,
                                        a.get("can_take_credit", True), a.get("member_limit_paise"))
            return t(lang, "did_add_member", name=c.name), {"customer": customer_card(c), "family_id": a["family_id"]}
        if pa.tool == "remove_member":
            c = await family.remove_member(s, m.id, uuid.UUID(a["member_id"]))
            return t(lang, "did_remove_member", name=c.name), {"customer": customer_card(c)}
        log = await family.merge_families(s, m.id, uuid.UUID(a["from_id"]), uuid.UUID(a["into_id"]), ctx.actor)
        return t(lang, "did_merge"), {"merge_id": str(log.id)}

    raise LedgerError(f"Unknown pending tool {pa.tool}")


async def approve(session: AsyncSession, merchant, action_id: uuid.UUID, approver: str) -> dict:
    """The owner approves a staff edit (§19.3): it is committed now, with approved_by recorded."""
    if approver != "owner":
        raise LedgerError.of("err_owner_only")
    pa = await session.get(PendingAction, action_id)
    if pa is None or pa.merchant_id != merchant.id or pa.status != PendingStatus.AWAITING_APPROVAL:
        raise NotFoundError.of("err_approval_not_found")
    ctx = ToolContext(session, merchant, pa.session_id, datetime.now(get_settings().tz).date(), actor="owner")
    async with session.begin_nested():
        phrase, result = await _commit_one(ctx, pa, {})
    pa.status, pa.result = PendingStatus.COMMITTED, result
    return {"action_id": str(pa.id), "status": "COMMITTED", "reply": phrase}


async def reject(session: AsyncSession, merchant, action_id: uuid.UUID, approver: str) -> dict:
    if approver != "owner":
        raise LedgerError.of("err_owner_only")
    pa = await session.get(PendingAction, action_id)
    if pa is None or pa.merchant_id != merchant.id or pa.status != PendingStatus.AWAITING_APPROVAL:
        raise NotFoundError.of("err_approval_not_found")
    pa.status = PendingStatus.CANCELLED
    return {"action_id": str(pa.id), "status": "CANCELLED"}


def _second_prompt(pa: PendingAction, lang: str) -> str:
    """Over a limit / no permission: the last "haan" before saving names exactly what is being overridden."""
    w = pa.args.get("credit_warning") if pa.tool == "add_credit" else None
    last = pa.confirmations_received == pa.confirmations_needed - 1
    if pa.tool == "add_credit" and pa.args.get("override_text") and last:
        return t(lang, "override_confirm", text=pa.args["override_text"], summary=pa.summary)
    if w and w.get("kind") == "over_limit" and last:
        return t(lang, "over_limit_confirm", limit=w["limit"], over=w["over_by"], summary=pa.summary)
    return t(lang, "big_confirm", summary=pa.summary)


async def confirm(ctx: ToolContext) -> ConfirmOutcome | None:
    """Merchant said yes. Commit every pending action of this session atomically."""
    lang = ctx.merchant.language
    actions = await active_pending(ctx.session, ctx.merchant.id, ctx.session_id)
    if not actions:
        return None
    # The first "haan" answers the read-back of every newly proposed action. After that, each "haan"
    # answers only the follow-up prompt that was just spoken (waiting[0]), so every extra confirmation
    # (big amount, over the limit) is asked by name and never satisfied by another action's "haan".
    fresh = [pa for pa in actions if pa.confirmations_received == 0]
    waiting = [pa for pa in actions if pa.confirmations_received < pa.confirmations_needed]
    for pa in fresh or waiting[:1]:
        pa.confirmations_received += 1
    waiting = [pa for pa in actions if pa.confirmations_received < pa.confirmations_needed]
    if waiting:
        return ConfirmOutcome(
            reply=_second_prompt(waiting[0], lang),
            outcome="awaiting_second_confirm",
        )

    # Staff edits are not saved on the staff's "haan": they wait for the owner (§19.3).
    held = [pa for pa in actions if (pa.args or {}).get("needs_approval")]
    for pa in held:
        pa.status = PendingStatus.AWAITING_APPROVAL
    actions = [pa for pa in actions if pa not in held]
    held_note = t(lang, "sent_for_approval", n=len(held)) if held else ""
    if not actions:
        return ConfirmOutcome(reply=held_note, outcome="awaiting_approval")

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
    reply = t(lang, "done") + " " + ". ".join(phrases) + "." + (" " + held_note if held_note else "")
    nudges = [c["nudge"] for c in committed if c.get("nudge")]
    if len(nudges) == 1:
        reply += " " + t(lang, "nudge_offer", amount=nudges[0]["remaining"])
    return ConfirmOutcome(reply=reply, committed=committed)


async def cancel(session: AsyncSession, merchant_id: uuid.UUID, session_id: str) -> int:
    actions = await active_pending(session, merchant_id, session_id)
    for pa in actions:
        pa.status = PendingStatus.CANCELLED
    return len(actions)
