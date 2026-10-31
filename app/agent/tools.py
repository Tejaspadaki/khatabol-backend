"""Tool schemas (what the LLM sees) and the executor (validated, typed calls into the ledger).

Read tools run immediately. Write tools only validate and create a PendingAction; nothing is
written to the khata until the merchant confirms (see pending.py).
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.i18n import t
from app.db.models import (
    Customer,
    EntryType,
    LedgerEntry,
    Merchant,
    PendingAction,
    PendingStatus,
    UpiTransaction,
    as_utc,
    utcnow,
)
from app.ledger import service as ledger
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.dates import resolve_due_date
from app.nlp.hinglish_numbers import format_rupees, rupees_to_paise
from app.payments import reconciler
from app.speech import spoken_date

NEW_PREFIX = "new:"


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "strict": True,
    }


# Kept short on purpose: every byte here is sent on every LLM call (Groq free tier: 7k input tokens/min).
_CUSTOMER = {"type": "string", "description": "Customer name as spoken, or a customer_id / 'new:<id>' from a result."}
_AMOUNT = {"type": "number", "description": "Rupees"}

TOOL_SCHEMAS: list[dict] = [
    _tool("find_customer", "Search customers by name; returns match / ambiguous / no_match with candidates.",
          {"name_query": {"type": "string"}, "phone": {"type": "string"}}, ["name_query"]),
    _tool("create_customer", "Propose a new customer. Returns placeholder_customer_id for use in this turn.",
          {"name": {"type": "string"}, "phone": {"type": "string"}, "nickname": {"type": "string"}}, ["name"]),
    _tool("add_credit", "Propose udhaar: merchant gave goods/money on credit to the customer.",
          {"customer": _CUSTOMER, "amount_rupees": _AMOUNT,
           "due": {"type": "string", "description": "Due date exactly as spoken ('somvar', '5 tareekh'); omit if none"},
           "items": {"type": "string"}, "note": {"type": "string"}},
          ["customer", "amount_rupees"]),
    _tool("record_payment", "Propose a payment the customer made to the merchant.",
          {"customer": _CUSTOMER, "amount_rupees": _AMOUNT, "mode": {"type": "string", "enum": ["cash", "upi", "other"]}},
          ["customer", "amount_rupees", "mode"]),
    _tool("get_balance", "Balance, next due, overdue and last payment of one customer.",
          {"customer": _CUSTOMER}, ["customer"]),
    _tool("get_history", "Recent entries of one customer.",
          {"customer": _CUSTOMER, "limit": {"type": "integer"}}, ["customer"]),
    _tool("list_dues", "Customers who owe money. today includes overdue; this_week = next 7 days + overdue.",
          {"filter": {"type": "string", "enum": ["today", "overdue", "this_week", "all"]}}, ["filter"]),
    _tool("link_upi", "Propose saving a customer's UPI ID (name@bank).",
          {"customer": _CUSTOMER, "vpa": {"type": "string"}}, ["customer", "vpa"]),
    _tool("send_reminder", "Propose a WhatsApp reminder with UPI link: one customer, or all_due=true.",
          {"customer": _CUSTOMER, "all_due": {"type": "boolean"}}, []),
    _tool("daily_summary", "Credit given, collected and outstanding for a day.",
          {"day": {"type": "string", "description": "'aaj', 'kal' or YYYY-MM-DD"}}, []),
    _tool("undo_last", "Propose reversing the most recent entry.", {}, []),
    _tool("cancel_pending", "Cancel an unconfirmed pending action.", {"action_id": {"type": "string"}}, ["action_id"]),
    _tool("list_unmatched_payments", "UPI payments from unknown payers.", {}, []),
    _tool("assign_upi_payment", "Propose assigning an unknown-payer UPI payment to a customer.",
          {"txn_id": {"type": "string"}, "customer": _CUSTOMER}, ["txn_id", "customer"]),
]

WRITE_TOOLS = {
    "create_customer", "add_credit", "record_payment", "link_upi", "send_reminder", "undo_last", "assign_upi_payment",
}


@dataclass
class ToolContext:
    session: AsyncSession
    merchant: Merchant
    session_id: str
    today: date
    voice_log_id: uuid.UUID | None = None
    proposed: list[PendingAction] = field(default_factory=list)


def to_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def customer_card(c: Customer) -> dict:
    return {
        "customer_id": str(c.id),
        "name": c.name,
        "phone_last4": c.phone[-4:] if c.phone else None,
        "nicknames": c.nicknames or [],
        "balance": format_rupees(c.balance_paise),
        "balance_paise": c.balance_paise,
    }


def entry_card(e: LedgerEntry, name: str | None = None) -> dict:
    return {
        "entry_id": str(e.id),
        "customer": name,
        "type": e.type.value,
        "amount": format_rupees(e.amount_paise),
        "due_date": e.due_date.isoformat() if e.due_date else None,
        "items": e.items,
        "source": e.source.value,
        "mode": e.payment_mode.value if e.payment_mode else None,
        "at": e.created_at.isoformat() if e.created_at else None,
    }


def _parse_uuid(raw: str, what: str = "customer_id") -> uuid.UUID:
    try:
        return uuid.UUID(str(raw).strip())
    except ValueError:
        raise LedgerError(f"{what} '{raw}' is not valid; get it from find_customer first.") from None


async def active_pending(session: AsyncSession, merchant_id: uuid.UUID, session_id: str) -> list[PendingAction]:
    """Unexpired pending actions for this conversation; expires stale ones as a side effect."""
    stmt = (
        select(PendingAction)
        .where(
            PendingAction.merchant_id == merchant_id,
            PendingAction.session_id == session_id,
            PendingAction.status == PendingStatus.PENDING,
        )
        .order_by(PendingAction.created_at)
    )
    now = utcnow()
    live = []
    for p in (await session.execute(stmt)).scalars():
        exp = p.expires_at if p.expires_at.tzinfo else p.expires_at.replace(tzinfo=now.tzinfo)
        if exp <= now:
            p.status = PendingStatus.EXPIRED
        else:
            live.append(p)
    return live


@dataclass
class Resolved:
    customer: Customer | None  # None for a 'new:<id>' placeholder
    name: str
    ref: str  # what to store in pending args: a customer UUID or the 'new:<id>' placeholder
    spoken_name: str | None = None  # set when the model passed a name, so it can be learned as a nickname


def _is_uuid(raw: str) -> bool:
    try:
        uuid.UUID(raw)
        return True
    except ValueError:
        return False


async def _resolve_customer(ctx: ToolContext, raw: str) -> Resolved:
    """Accept a customer_id, a 'new:<id>' placeholder, or the name as spoken.

    A name is resolved only when it is a clear match; otherwise the error lists the candidates so the
    model asks the merchant instead of guessing.
    """
    raw = " ".join(str(raw).split())
    if raw.startswith(NEW_PREFIX):
        action_id = _parse_uuid(raw[len(NEW_PREFIX):], "placeholder")
        pa = await ctx.session.get(PendingAction, action_id)
        if pa is None or pa.tool != "create_customer" or pa.status != PendingStatus.PENDING:
            raise LedgerError(f"Placeholder {raw} is not a pending new customer.")
        return Resolved(None, pa.args["name"], raw)
    if _is_uuid(raw):
        c = await ledger.get_customer(ctx.session, ctx.merchant.id, uuid.UUID(raw))
        return Resolved(c, c.name, str(c.id))

    found = await ledger.find_customers(ctx.session, ctx.merchant.id, raw)
    if found["decision"] == "match":
        c = found["matches"][0]["customer"]
        return Resolved(c, c.name, str(c.id), spoken_name=raw)
    if found["decision"] == "ambiguous":
        options = "; ".join(
            f"{m['customer'].name} (customer_id {m['customer'].id}, "
            f"phone ..{(m['customer'].phone or '----')[-4:]}, baaki {format_rupees(m['customer'].balance_paise)})"
            for m in found["matches"]
        )
        raise LedgerError(f"'{raw}' naam ke kai customer hain: {options}. Merchant se poochiye kaun sa, "
                          "phir customer_id ke saath dobara call kijiye.")
    raise LedgerError(f"'{raw}' naam ka koi customer nahi hai. Merchant se poochiye: "
                      f"'Naya customer banaun, {raw} naam se?'")


def _amount_paise(value) -> int:
    try:
        paise = rupees_to_paise(float(value))
    except (TypeError, ValueError):
        raise LedgerError("amount_rupees must be a number.") from None
    if paise <= 0:
        raise LedgerError("Amount zero se zyada hona chahiye.")
    if paise > ledger.MAX_AMOUNT_PAISE:
        raise LedgerError("Amount bahut bada hai, dobara poochiye.")
    return paise


async def _propose(ctx: ToolContext, tool: str, args: dict, summary: str, confirmations: int = 1) -> dict:
    ttl = get_settings().pending_action_ttl_seconds
    pa = PendingAction(
        merchant_id=ctx.merchant.id,
        session_id=ctx.session_id,
        tool=tool,
        args=args,
        summary=summary,
        confirmations_needed=confirmations,
        voice_log_id=ctx.voice_log_id,
        expires_at=utcnow() + timedelta(seconds=ttl),
    )
    ctx.session.add(pa)
    await ctx.session.flush()
    ctx.proposed.append(pa)
    result = {
        "status": "pending_confirmation",
        "action_id": str(pa.id),
        "summary": summary,
        "needs_double_confirmation": confirmations > 1,
    }
    if tool == "create_customer":
        result["placeholder_customer_id"] = f"{NEW_PREFIX}{pa.id}"
    return result


# ---------------------------------------------------------------- read tools

async def _find_customer(ctx: ToolContext, name_query: str, phone: str | None = None) -> dict:
    found = await ledger.find_customers(ctx.session, ctx.merchant.id, name_query, phone)
    matches = []
    for m in found["matches"]:
        card = customer_card(m["customer"])
        card["score"] = m["score"]
        matches.append(card)
    return {"decision": found["decision"], "query": name_query, "matches": matches}


async def _get_balance(ctx: ToolContext, customer: str) -> dict:
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        raise LedgerError(f"{r.name} abhi save nahi hua, unka hisaab khaali hai.")
    b = await ledger.get_balance(ctx.session, ctx.merchant.id, r.customer.id, ctx.today)
    c, lp = b["customer"], b["last_payment"]
    return {
        "name": c.name,
        "balance": format_rupees(b["balance_paise"]),
        "balance_paise": b["balance_paise"],
        "advance": b["balance_paise"] < 0,
        "next_due_date": b["next_due_date"].isoformat() if b["next_due_date"] else None,
        "next_due_spoken": spoken_date(b["next_due_date"], ctx.today, ctx.merchant.language) if b["next_due_date"] else None,
        "next_due_amount": format_rupees(b["next_due_paise"]) if b["next_due_date"] else None,
        "overdue_amount": format_rupees(b["overdue_paise"]) if b["overdue_paise"] else None,
        "last_payment": {"amount": format_rupees(lp.amount_paise),
                         "on": as_utc(lp.created_at).astimezone(get_settings().tz).date().isoformat()}
        if lp else None,
    }


async def _get_history(ctx: ToolContext, customer: str, limit: int = 5) -> dict:
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        return {"name": r.name, "entries": []}
    entries = await ledger.get_history(ctx.session, ctx.merchant.id, r.customer.id, max(1, min(int(limit or 5), 20)))
    return {"name": r.name, "entries": [entry_card(e) for e in entries]}


async def _list_dues(ctx: ToolContext, filter: str) -> dict:
    rows = await ledger.list_dues(ctx.session, ctx.merchant.id, filter, ctx.today)
    return {
        "filter": filter,
        "count": len(rows),
        "total_due": format_rupees(sum(r["due_paise"] for r in rows)),
        "customers": [
            {
                "customer_id": str(r["customer"].id),
                "name": r["customer"].name,
                "due": format_rupees(r["due_paise"]),
                "balance": format_rupees(r["balance_paise"]),
                "overdue": format_rupees(r["overdue_paise"]) if r["overdue_paise"] else None,
                "next_due_date": r["next_due_date"].isoformat() if r["next_due_date"] else None,
            }
            for r in rows[:10]
        ],
    }


async def _daily_summary(ctx: ToolContext, day: str | None = None) -> dict:
    d = resolve_due_date(day, ctx.today) if day else ctx.today
    if day and d is None:
        raise LedgerError(f"Date '{day}' samajh nahi aayi.")
    if day and day.strip().lower() in {"kal", "yesterday"}:  # for a summary, "kal" means yesterday
        d = ctx.today - timedelta(days=1)
    s = await ledger.daily_summary(ctx.session, ctx.merchant.id, d)
    return {
        "date": d.isoformat(),
        "credit_given": format_rupees(s["credit_given_paise"]),
        "collected": format_rupees(s["collected_paise"]),
        "collected_upi": format_rupees(s["collected_upi_paise"]),
        "entries": s["entries"],
        "total_outstanding": format_rupees(s["outstanding_paise"]),
    }


async def _list_unmatched(ctx: ToolContext) -> dict:
    txns = await reconciler.list_unmatched(ctx.session, ctx.merchant.id)
    return {
        "payments": [
            {"txn_id": t.txn_id, "payer_vpa": t.payer_vpa, "payer_name": t.payer_name,
             "amount": format_rupees(t.amount_paise), "at": t.paid_at.isoformat()}
            for t in txns
        ]
    }


async def _cancel_pending(ctx: ToolContext, action_id: str) -> dict:
    pa = await ctx.session.get(PendingAction, _parse_uuid(action_id, "action_id"))
    if pa is None or pa.merchant_id != ctx.merchant.id or pa.status != PendingStatus.PENDING:
        raise LedgerError("No such pending action.")
    pa.status = PendingStatus.CANCELLED
    ctx.proposed = [p for p in ctx.proposed if p.id != pa.id]
    return {"cancelled": action_id, "summary": pa.summary}


# ---------------------------------------------------------------- write proposals

async def _create_customer(ctx: ToolContext, name: str, phone: str | None = None, nickname: str | None = None):
    name = " ".join(name.split())
    if not name:
        raise LedgerError("Naam khaali hai.")
    summary = t(ctx.merchant.language, "sum_new_customer", name=name) + (f", phone {phone}" if phone else "")
    return await _propose(ctx, "create_customer", {"name": name, "phone": phone, "nickname": nickname}, summary)


async def _add_credit(ctx: ToolContext, customer: str, amount_rupees, due: str | None = None,
                      items: str | None = None, note: str | None = None):
    r = await _resolve_customer(ctx, customer)
    name = r.name
    paise = _amount_paise(amount_rupees)
    due_date = None
    if due:
        due_date = resolve_due_date(due, ctx.today, get_settings().salary_day)
        if due_date is None:
            raise LedgerError(f"Due date '{due}' samajh nahi aayi. Merchant se tareekh poochiye.")
        if due_date < ctx.today:
            raise LedgerError(f"Due date {due_date} beet chuki hai. Merchant se dobara poochiye.")
    lang = ctx.merchant.language
    summary = t(lang, "sum_credit", name=name, amount=format_rupees(paise))
    if items:
        summary += f" ({items})"
    if due_date:
        summary += t(lang, "sum_due", date=spoken_date(due_date, ctx.today, lang))
    big = paise > ctx.merchant.large_amount_threshold_paise
    args = {"customer_id": r.ref, "amount_paise": paise, "due_date": due_date.isoformat() if due_date else None,
            "items": items, "note": note, "spoken_name": r.spoken_name}
    return await _propose(ctx, "add_credit", args, summary, 2 if big else 1)


async def _record_payment(ctx: ToolContext, customer: str, amount_rupees, mode: str = "cash"):
    r = await _resolve_customer(ctx, customer)
    name = r.name
    paise = _amount_paise(amount_rupees)
    mode = (mode or "cash").lower()
    if mode not in {"cash", "upi", "other"}:
        raise LedgerError("mode must be cash, upi or other.")
    lang = ctx.merchant.language
    mode_word = t(lang, f"mode_{mode}")
    summary = " ".join(t(lang, "sum_payment", name=name, amount=format_rupees(paise), mode=mode_word).split())
    big = paise > ctx.merchant.large_amount_threshold_paise
    args = {"customer_id": r.ref, "amount_paise": paise, "mode": mode, "spoken_name": r.spoken_name}
    return await _propose(ctx, "record_payment", args, summary, 2 if big else 1)


async def _link_upi(ctx: ToolContext, customer: str, vpa: str):
    r = await _resolve_customer(ctx, customer)
    vpa = vpa.strip().lower().replace(" at ", "@").replace(" ", "")
    if "@" not in vpa:
        raise LedgerError("UPI ID mein @ hona chahiye (jaise naam@ybl). Merchant se dobara poochiye.")
    return await _propose(ctx, "link_upi", {"customer_id": r.ref, "vpa": vpa},
                          t(ctx.merchant.language, "sum_link_upi", name=r.name, vpa=vpa))


async def _send_reminder(ctx: ToolContext, customer: str | None = None, all_due: bool | str = False):
    all_due = str(all_due).strip().lower() in {"true", "1", "yes"}  # models sometimes send "false" as a string
    if not ctx.merchant.upi_vpa:
        raise LedgerError("Merchant ki UPI ID set nahi hai, reminder link nahi ban sakta.")
    if customer:
        r = await _resolve_customer(ctx, customer)
        if r.customer is None or r.customer.balance_paise <= 0:
            raise LedgerError(f"{r.name} ka kuch baaki nahi hai.")
        summary = t(ctx.merchant.language, "sum_reminder_one", name=r.name,
                    amount=format_rupees(r.customer.balance_paise))
        return await _propose(ctx, "send_reminder", {"customer_id": r.ref}, summary)
    if all_due:
        rows = await ledger.list_dues(ctx.session, ctx.merchant.id, "today", ctx.today)
        if not rows:
            raise LedgerError("Aaj kisi ka paisa due nahi hai.")
        total = format_rupees(sum(r["due_paise"] for r in rows))
        summary = t(ctx.merchant.language, "sum_reminder_all", n=len(rows), total=total)
        return await _propose(ctx, "send_reminder", {"all_due": True}, summary)
    raise LedgerError("Give customer or all_due=true.")


async def _undo_last(ctx: ToolContext):
    e = await ledger.last_reversible_entry(ctx.session, ctx.merchant.id)
    if e is None:
        raise LedgerError("Cancel karne ke liye koi entry nahi hai.")
    c = await ledger.get_customer(ctx.session, ctx.merchant.id, e.customer_id)
    lang = ctx.merchant.language
    kind = t(lang, "kind_credit" if e.type == EntryType.CREDIT else "kind_payment")
    summary = t(lang, "sum_undo", name=c.name, amount=format_rupees(e.amount_paise), kind=kind)
    return await _propose(ctx, "undo_last", {"entry_id": str(e.id)}, summary)


async def _assign_upi_payment(ctx: ToolContext, txn_id: str, customer: str):
    r = await _resolve_customer(ctx, customer)
    stmt = select(UpiTransaction).where(UpiTransaction.merchant_id == ctx.merchant.id, UpiTransaction.txn_id == txn_id)
    txn = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if txn is None:
        raise NotFoundError("Ye payment nahi mila; list_unmatched_payments se txn_id lijiye.")
    summary = t(ctx.merchant.language, "sum_assign", amount=format_rupees(txn.amount_paise),
                vpa=txn.payer_vpa, name=r.name)
    return await _propose(ctx, "assign_upi_payment", {"txn_id": txn_id, "customer_id": r.ref}, summary)


HANDLERS = {
    "find_customer": _find_customer,
    "get_balance": _get_balance,
    "get_history": _get_history,
    "list_dues": _list_dues,
    "daily_summary": _daily_summary,
    "list_unmatched_payments": _list_unmatched,
    "cancel_pending": _cancel_pending,
    "create_customer": _create_customer,
    "add_credit": _add_credit,
    "record_payment": _record_payment,
    "link_upi": _link_upi,
    "send_reminder": _send_reminder,
    "undo_last": _undo_last,
    "assign_upi_payment": _assign_upi_payment,
}


async def execute_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        raise LedgerError(f"Unknown tool {name}.")
    allowed = set(next(t for t in TOOL_SCHEMAS if t["name"] == name)["input_schema"]["properties"])
    clean = {k: v for k, v in (args or {}).items() if k in allowed and v is not None and v != ""}
    try:
        return await handler(ctx, **clean)
    except TypeError as exc:  # missing required argument
        raise LedgerError(f"Bad arguments for {name}: {exc}") from None
