"""Tool schemas (what the LLM sees) and the executor (validated, typed calls into the ledger).

Read tools run immediately. Write tools only validate and create a PendingAction; nothing is
written to the khata until the merchant confirms (see pending.py).
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from rapidfuzz import fuzz
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
from app.ledger import edits
from app.ledger import service as ledger
from app.ledger.allocation import effective_entries
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.dates import resolve_due_date
from app.nlp.hinglish_numbers import format_rupees, normalize_amounts, rupees_to_paise
from app.nlp.names import normalize_name
from app.payments import reconciler
from app.risk import trust_score
from app.scheduler.reminders import nudge_sent
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
           "family": {"type": "string", "description": "family_id or name when no member is named"},
           "due": {"type": "string", "description": "Due date exactly as spoken ('somvar', '5 tareekh'); omit if none"},
           "items": {"type": "string"}, "note": {"type": "string"}},
          ["customer", "amount_rupees"]),
    _tool("record_payment", "Propose a payment the customer made to the merchant.",
          {"customer": _CUSTOMER, "amount_rupees": _AMOUNT, "mode": {"type": "string", "enum": ["cash", "upi", "other"]},
           "for_entry": {"type": "string", "description": "Udhaar it pays, as spoken ('Diwali wala bill', '500 wala'); "
                                                         "omit = oldest due first"}},
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
    _tool("set_credit_limit", "Propose a customer's udhaar limit ('X ki limit 2000 rakho'). 0 = no new udhaar.",
          {"customer": _CUSTOMER, "limit_rupees": _AMOUNT}, ["customer", "limit_rupees"]),
    _tool("get_credit_status", "Balance, udhaar limit and percent of the limit used for one customer.",
          {"customer": _CUSTOMER}, ["customer"]),
    _tool("set_hold", "Propose putting a customer on hold (no new udhaar) or lifting the hold.",
          {"customer": _CUSTOMER, "on_hold": {"type": "boolean"}, "reason": {"type": "string"}},
          ["customer", "on_hold"]),
    _tool("send_partial_nudge", "Propose a WhatsApp reminder for what is still due after a payment.",
          {"customer": _CUSTOMER, "payment_id": {"type": "string", "description": "Omit for the latest payment"}},
          ["customer"]),
    _tool("edit_entry", "Propose fixing a SAVED entry: amount, due date (or extend_days), items, customer, note, or "
          "cancel. Omit entry/customer = the latest entry.",
          {"entry": {"type": "string", "description": "entry_id, or as spoken ('500 wali', 'aata wali')"},
           "customer": _CUSTOMER, "amount_rupees": _AMOUNT, "due": {"type": "string"},
           "extend_days": {"type": "integer", "description": "'10 din aur de do' = 10"}, "items": {"type": "string"},
           "to_customer": {"type": "string", "description": "Move the entry to this customer"},
           "note": {"type": "string"}, "cancel": {"type": "boolean"}, "reason": {"type": "string"}}, []),
    _tool("waive_amount", "Propose a waiver / discount ('Gupta ji ke 50 chhod do'): lowers what is owed, not a payment.",
          {"customer": _CUSTOMER, "amount_rupees": _AMOUNT, "reason": {"type": "string"},
           "for_entry": {"type": "string"}}, ["customer", "amount_rupees"]),
    _tool("resolve_party", "Who is meant: a person, 'X ke bete' (anchor + relation), or a family ('Sharma parivar').",
          {"person_name": {"type": "string"}, "anchor_name": {"type": "string"},
           "relation": {"type": "string", "description": "beta, beti, bahu, patni, pati, bhai, maa ..."},
           "family_hint": {"type": "string"}}, []),
    _tool("find_family", "Search families by name or head's name.", {"name": {"type": "string"}}, ["name"]),
    _tool("create_family", "Propose a family khata with a head customer.",
          {"name": {"type": "string"}, "head": _CUSTOMER, "area_hint": {"type": "string"},
           "limit_rupees": _AMOUNT}, ["name", "head"]),
    _tool("add_member", "Propose adding a member (new name or existing customer) to a family.",
          {"family": {"type": "string"}, "name": {"type": "string"}, "relation": {"type": "string"},
           "phone": {"type": "string"}, "can_take_credit": {"type": "boolean"}, "member_limit_rupees": _AMOUNT},
          ["family", "name"]),
    _tool("remove_member", "Propose a member leaving the family (past entries stay with the family).",
          {"member": _CUSTOMER}, ["member"]),
    _tool("get_family_balance", "Family total and per-member breakdown ('kisne kitna liya').",
          {"family": {"type": "string"}}, ["family"]),
    _tool("merge_families", "Propose merging a duplicate family into another.",
          {"family": {"type": "string"}, "into_family": {"type": "string"}}, ["family", "into_family"]),
    _tool("check_udhaar_risk", "Trust Score: should the shop give udhaar to this customer / UPI ID? Advice only.",
          {"customer": _CUSTOMER, "vpa": {"type": "string"},
           "recent_payment_rupees": {"type": "number", "description": "'jo abhi ₹50 aaye the' = 50"},
           "name": {"type": "string", "description": "Name the person gave, for the UPI name check"},
           "amount_rupees": _AMOUNT}, []),
]

WRITE_TOOLS = {
    "create_customer", "add_credit", "record_payment", "link_upi", "send_reminder", "undo_last", "assign_upi_payment",
    "set_credit_limit", "set_hold", "send_partial_nudge", "edit_entry", "waive_amount",
    "create_family", "add_member", "remove_member", "merge_families",
}

# Edits (§19): staff edits wait for the owner's approval after their "haan".
EDIT_TOOLS = {"edit_entry", "waive_amount", "undo_last"}

# Tools the app may propose directly from a button (POST /agent/propose); "haan" still goes through the gate.
DIRECT_TOOLS = {"send_partial_nudge", "edit_entry", "waive_amount", "create_family", "add_member", "remove_member",
                "merge_families"}


@dataclass
class ToolContext:
    session: AsyncSession
    merchant: Merchant
    session_id: str
    today: date
    voice_log_id: uuid.UUID | None = None
    proposed: list[PendingAction] = field(default_factory=list)
    actor: str = "owner"  # 'owner' or 'staff:<id>' (§19.3)
    spoken_party: dict = field(default_factory=dict)  # member_id -> how resolve_party heard them (nickname learning)


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
        "on_hold": bool(c.on_hold),
        "hold_reason": c.hold_reason,
    }


def entry_card(e: LedgerEntry, name: str | None = None, status: tuple[str, int] | None = None) -> dict:
    """status: (OPEN | PARTIAL | PAID | REVERSED, remaining paise) for udhaar entries (plan.md §20)."""
    card = {
        "entry_id": str(e.id),
        "customer": name,
        "type": e.type.value,
        "amount": format_rupees(e.amount_paise),
        "due_date": e.due_date.isoformat() if e.due_date else None,
        "items": e.items,
        "source": e.source.value,
        "mode": e.payment_mode.value if e.payment_mode else None,
        "at": e.created_at.isoformat() if e.created_at else None,
        "reason": e.reason,
    }
    if status:
        card["status"], card["remaining"] = status[0], format_rupees(status[1])
    return card


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
    if tool in EDIT_TOOLS:
        args = {**args, "edited_by": ctx.actor, "needs_approval": ctx.actor != "owner"}
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
        "on_hold": bool(c.on_hold),
        "hold_reason": c.hold_reason,
    }


async def _get_history(ctx: ToolContext, customer: str, limit: int = 5) -> dict:
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        return {"name": r.name, "entries": []}
    entries = await ledger.get_history(ctx.session, ctx.merchant.id, r.customer.id, max(1, min(int(limit or 5), 20)))
    st = await ledger.entry_statuses(ctx.session, r.customer.id)
    return {"name": r.name, "entries": [entry_card(e, status=st.get(e.id)) for e in entries]}


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


async def _add_credit(ctx: ToolContext, customer: str | None = None, amount_rupees=None, due: str | None = None,
                      items: str | None = None, note: str | None = None, family: str | None = None):
    fam_level = None
    if family and not customer:  # "Sharma parivar mein 300 likho"
        fam_level = await _resolve_family(ctx, family)
        customer = str(fam_level.head_customer_id)
    if not customer:
        raise LedgerError("Kisko udhaar? Customer ya parivar bataiye.")
    r = await _resolve_customer(ctx, customer)
    if r.customer is not None and str(r.customer.id) in ctx.spoken_party:
        r.spoken_name = ctx.spoken_party[str(r.customer.id)]
    account = fam = member = None
    if r.customer is not None:
        account, fam, member = await ledger.khata_route(ctx.session, ctx.merchant.id, r.customer)
        if fam_level is not None:
            member = None
        r = Resolved(account, r.name, r.ref, r.spoken_name) if fam else r
    if r.customer is not None and r.customer.on_hold:
        raise ledger.hold_refusal(r.customer)  # the tool refuses; the agent only speaks it
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
    if fam is not None:
        summary = t(lang, "sum_credit_family", family=fam.display_name, member=member.name if member else "-",
                    amount=format_rupees(paise))
    if items:
        summary += f" ({items})"
    if due_date:
        summary += t(lang, "sum_due", date=spoken_date(due_date, ctx.today, lang))
    warning, over_by = await _limit_check(ctx, r, paise)
    overrides: list[str] = []
    if warning and warning["kind"] == "over_limit":
        summary += t(lang, "sum_over_limit", limit=warning["limit"], over=warning["over_by"])
        overrides.append(t(lang, "ovr_limit", limit=warning["limit"], over=warning["over_by"]))
    elif warning:
        summary += t(lang, "sum_near_limit", limit=warning["limit"], pct=warning["percent_used"])
    fam_allow = await _family_check(ctx, account, fam, member, paise, overrides) if fam else {}
    if fam_allow:
        summary += " (" + "; ".join(overrides[1 if over_by else 0:]) + ")"
    guard = None
    if r.customer is not None:  # Udhaar Guard (plan.md §22): advice in the read-back, never a block
        a = await trust_score.assess(ctx.session, ctx.merchant, ctx.today, customer=r.customer)
        guard = {"level": a.level, "suggested_limit": format_rupees(a.suggested_paise),
                 "warn": a.level == "low" and paise > a.suggested_paise}
        if guard["warn"]:
            summary += t(lang, "sum_trust_low", amount=guard["suggested_limit"])
    big = paise > ctx.merchant.large_amount_threshold_paise
    args = {"customer_id": r.ref, "amount_paise": paise, "due_date": due_date.isoformat() if due_date else None,
            "items": items, "note": note, "spoken_name": r.spoken_name,
            "over_limit_paise": over_by, "credit_warning": warning, "trust_score": guard,
            "family_level": fam_level is not None, "override_text": "; ".join(overrides) or None, **fam_allow}
    # Going over a limit (or a member without permission) takes one more "haan", asked last and naming it.
    return await _propose(ctx, "add_credit", args, summary, (2 if big else 1) + (1 if overrides else 0))


async def _family_check(ctx: ToolContext, account: Customer, fam, member, paise: int, overrides: list[str]) -> dict:
    """Preview of the write path's family guardrails (§17.4); the service checks again on commit."""
    lang, allow = ctx.merchant.language, {}
    if member is not None and not member.can_take_credit:
        overrides.append(t(lang, "ovr_no_permission", name=member.name))
        allow["allow_no_permission"] = True
    if member is not None and member.member_limit_paise is not None:
        over = await ledger.member_outstanding(ctx.session, account.id, member.id) + paise - member.member_limit_paise
        if over > 0:
            overrides.append(t(lang, "ovr_member", name=member.name, limit=format_rupees(member.member_limit_paise),
                               over=format_rupees(over)))
            allow["allow_member_over_paise"] = over
    if fam.credit_limit_paise is not None:
        over = (account.balance_paise or 0) + paise - fam.credit_limit_paise
        if over > 0:
            overrides.append(t(lang, "ovr_family", name=fam.display_name, limit=format_rupees(fam.credit_limit_paise),
                               over=format_rupees(over)))
            allow["allow_family_over_paise"] = over
    return allow


async def _limit_check(ctx: ToolContext, r: Resolved, paise: int) -> tuple[dict | None, int]:
    """The tool decides, not the model: refuse at a zero limit, else return (card warning, overage).

    Other udhaar for the same customer still waiting in this conversation counts too, since a "haan"
    commits them together.
    """
    c = r.customer
    if c is None or c.credit_limit_paise is None:
        return None, 0
    if c.credit_limit_paise == 0:
        raise LedgerError.of("err_limit_zero", name=c.name)
    waiting = sum(p.args["amount_paise"] for p in await active_pending(ctx.session, ctx.merchant.id, ctx.session_id)
                  if p.tool == "add_credit" and p.args.get("customer_id") == r.ref)
    st = ledger.credit_status(c, waiting + paise)
    limit = format_rupees(c.credit_limit_paise)
    if st["over_by_paise"]:
        return {"kind": "over_limit", "over_by": format_rupees(st["over_by_paise"]), "limit": limit}, st["over_by_paise"]
    if st["near_limit"]:
        return {"kind": "near_limit", "percent_used": st["used_pct"], "limit": limit}, 0
    return None, 0


def _entry_label(e: LedgerEntry, remaining: int | None = None) -> str:
    bits = [e.items or e.note, format_rupees(e.amount_paise),
            f"due {e.due_date.isoformat()}" if e.due_date else None,
            f"baaki {format_rupees(remaining)}" if remaining is not None else None]
    return ", ".join(b for b in bits if b)


def _choose_entry(spoken: str, cands: list[tuple[LedgerEntry, int | None]], what: str) -> LedgerEntry:
    """Pick the entry the merchant named: an entry_id, an amount ("500 wala") or its items ("Diwali")."""
    spoken = " ".join(str(spoken).split())
    if _is_uuid(spoken):
        found = [x for x in cands if str(x[0].id) == spoken]
    else:
        numbers = {int(n) * 100 for n in re.findall(r"\d+", normalize_amounts(spoken)[0])}
        found = [x for x in cands if x[0].amount_paise in numbers]
        if not found:
            words = normalize_name(re.sub(r"\b(wala|wali|wale|bill|ka|ki|ke|entry|udhaar)\b", " ", spoken.lower()))
            found = [x for x in cands if words and fuzz.partial_ratio(
                words, normalize_name(f"{x[0].items or ''} {x[0].note or ''}")) >= 80]
    if len(found) == 1:
        return found[0][0]
    options = "; ".join(f"{_entry_label(e, left)} (entry_id {e.id})" for e, left in (found or cands))
    if not found:
        raise LedgerError(f"'{spoken}' jaisi koi {what} nahi mili. Entries: {options or 'koi nahi'}.")
    raise LedgerError(f"Kai entries match hui: {options}. Merchant se poochiye kaunsi, phir entry_id ke saath call kijiye.")


async def _match_open_entry(ctx: ToolContext, c: Customer, spoken: str) -> LedgerEntry:
    cands = [(o.entry, o.remaining_paise) for o in await ledger.open_credits_for(ctx.session, c.id)]
    return _choose_entry(spoken, cands, "khuli udhaar")


async def _pick_entry(ctx: ToolContext, entry: str | None, customer: str | None) -> LedgerEntry:
    """The saved entry to edit: an entry_id; else the named / latest live entry of the customer or the shop."""
    if entry and _is_uuid(str(entry).strip()):
        return await edits.live_entry(ctx.session, ctx.merchant.id, uuid.UUID(str(entry).strip()))
    if customer:
        r = await _resolve_customer(ctx, customer)
        if r.customer is None:
            raise LedgerError(f"{r.name} abhi save nahi hua, unki koi entry nahi hai.")
        rows = await ledger.customer_entries(ctx.session, r.customer.id)
    else:
        stmt = (select(LedgerEntry).where(LedgerEntry.merchant_id == ctx.merchant.id)
                .order_by(LedgerEntry.created_at.desc()).limit(40))
        rows = list((await ctx.session.execute(stmt)).scalars())
    gone = {e.reverses_entry_id for e in rows if e.type == EntryType.REVERSAL}
    live = sorted((e for e in rows if e.type in edits.EDITABLE and e.id not in gone),
                  key=lambda e: as_utc(e.created_at), reverse=True)
    if not live:
        raise LedgerError("Badalne ke liye koi entry nahi mili.")
    return _choose_entry(entry, [(e, None) for e in live], "entry") if entry else live[0]


_DEFAULT_REASON = {"amount": "galat amount", "due": "aur samay", "items": "galat saamaan",
                   "customer": "galat customer", "note": "note", "cancel": "cancel"}


async def _edit_entry(ctx: ToolContext, entry: str | None = None, customer: str | None = None, amount_rupees=None,
                      due: str | None = None, extend_days=None, items: str | None = None, to_customer: str | None = None,
                      note: str | None = None, cancel: bool | str = False, reason: str | None = None):
    original = await _pick_entry(ctx, entry, customer)
    c = await ledger.get_customer(ctx.session, ctx.merchant.id, original.customer_id)
    lang = ctx.merchant.language
    args: dict = {"entry_id": str(original.id)}
    changes, first = [], None
    if str(cancel).strip().lower() in {"true", "1", "yes"}:
        args["cancel"], first = True, "cancel"
        changes.append(t(lang, "chg_cancel"))
    else:
        if amount_rupees is not None:
            paise = _amount_paise(amount_rupees)
            if paise != original.amount_paise:
                args["amount_paise"], first = paise, first or "amount"
                changes.append(f"{format_rupees(original.amount_paise)} → {format_rupees(paise)}")
        if due or extend_days:
            if original.type != EntryType.CREDIT:
                raise LedgerError("Due date sirf udhaar entry ki hoti hai.")
            if extend_days:
                new_due = (original.due_date or ctx.today) + timedelta(days=int(extend_days))
            else:
                new_due = resolve_due_date(due, ctx.today, get_settings().salary_day)
                if new_due is None:
                    raise LedgerError(f"Due date '{due}' samajh nahi aayi.")
            if new_due < ctx.today:
                raise LedgerError(f"Due date {new_due} beet chuki hai.")
            old = spoken_date(original.due_date, ctx.today, lang) if original.due_date else "-"
            args.update(set_due=True, due_date=new_due.isoformat())
            first = first or "due"
            changes.append(t(lang, "chg_due", old=old, new=spoken_date(new_due, ctx.today, lang)))
        if items:
            args.update(set_items=True, items=items)
            first = first or "items"
            changes.append(t(lang, "chg_items", old=original.items or "-", new=items))
        if to_customer:
            r2 = await _resolve_customer(ctx, to_customer)
            if r2.customer is None:
                raise LedgerError(f"{r2.name} abhi save nahi hua; pehle khata kholiye.")
            if r2.customer.id != c.id:
                args["to_customer_id"], first = str(r2.customer.id), first or "customer"
                changes.append(f"{c.name} → {r2.customer.name}")
        if note:
            args["note"], first = " ".join(note.split()), first or "note"
            changes.append(t(lang, "chg_note", note=args["note"]))
    if not changes:
        raise LedgerError.of("err_nothing_to_change")
    args["reason"] = " ".join((reason or "").split()) or _DEFAULT_REASON[first]
    kind = t(lang, {EntryType.CREDIT: "kind_credit", EntryType.PAYMENT: "kind_payment"}.get(original.type, "kind_waiver"))
    summary = t(lang, "sum_edit", name=c.name, amount=format_rupees(original.amount_paise), kind=kind,
                changes="; ".join(changes), reason=args["reason"])
    return await _propose(ctx, "edit_entry", args, summary, 2 if edits.is_old(original, ctx.today) else 1)


async def _waive_amount(ctx: ToolContext, customer: str, amount_rupees, reason: str | None = None,
                        for_entry: str | None = None):
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        raise LedgerError(f"{r.name} abhi save nahi hua.")
    paise = _amount_paise(amount_rupees)
    if paise > max(r.customer.balance_paise, 0):
        raise LedgerError.of("err_waive_too_much", name=r.name, amount=format_rupees(max(r.customer.balance_paise, 0)))
    target = await _match_open_entry(ctx, r.customer, for_entry) if for_entry else None
    reason = " ".join((reason or "").split()) or "chhoot"
    summary = t(ctx.merchant.language, "sum_waive", name=r.name, amount=format_rupees(paise), reason=reason)
    return await _propose(ctx, "waive_amount", {"customer_id": r.ref, "amount_paise": paise, "reason": reason,
                                                "target_entry_id": str(target.id) if target else None}, summary)


async def _record_payment(ctx: ToolContext, customer: str, amount_rupees, mode: str = "cash",
                          for_entry: str | None = None):
    r = await _resolve_customer(ctx, customer)
    name = r.name
    paise = _amount_paise(amount_rupees)
    mode = (mode or "cash").lower()
    if mode not in {"cash", "upi", "other"}:
        raise LedgerError("mode must be cash, upi or other.")
    lang = ctx.merchant.language
    mode_word = t(lang, f"mode_{mode}")
    summary = " ".join(t(lang, "sum_payment", name=name, amount=format_rupees(paise), mode=mode_word).split())
    target = None
    if for_entry:
        if r.customer is None:
            raise LedgerError(f"{r.name} abhi save nahi hua, unki koi udhaar entry nahi hai.")
        target = await _match_open_entry(ctx, r.customer, for_entry)
        summary += t(lang, "sum_for_entry", entry=_entry_label(target))
    big = paise > ctx.merchant.large_amount_threshold_paise
    args = {"customer_id": r.ref, "amount_paise": paise, "mode": mode, "spoken_name": r.spoken_name,
            "target_entry_id": str(target.id) if target else None}
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
    return await _propose(ctx, "undo_last", {"entry_id": str(e.id)}, summary, 2 if edits.is_old(e, ctx.today) else 1)


async def _assign_upi_payment(ctx: ToolContext, txn_id: str, customer: str):
    r = await _resolve_customer(ctx, customer)
    stmt = select(UpiTransaction).where(UpiTransaction.merchant_id == ctx.merchant.id, UpiTransaction.txn_id == txn_id)
    txn = (await ctx.session.execute(stmt)).scalar_one_or_none()
    if txn is None:
        raise NotFoundError("Ye payment nahi mila; list_unmatched_payments se txn_id lijiye.")
    summary = t(ctx.merchant.language, "sum_assign", amount=format_rupees(txn.amount_paise),
                vpa=txn.payer_vpa, name=r.name)
    return await _propose(ctx, "assign_upi_payment", {"txn_id": txn_id, "customer_id": r.ref}, summary)


async def _set_credit_limit(ctx: ToolContext, customer: str, limit_rupees):
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        raise LedgerError(f"{r.name} abhi save nahi hua; pehle khata kholiye, phir limit.")
    try:
        paise = rupees_to_paise(float(limit_rupees))
    except (TypeError, ValueError):
        raise LedgerError("limit_rupees must be a number.") from None
    if paise < 0:
        raise LedgerError.of("err_limit_negative")
    if paise > ledger.MAX_AMOUNT_PAISE:
        raise LedgerError.of("err_amount_too_big")
    lang = ctx.merchant.language
    summary = t(lang, "sum_credit_limit", name=r.name, amount=format_rupees(paise))
    if r.customer.credit_limit_paise is not None:
        summary += t(lang, "sum_limit_was", amount=format_rupees(r.customer.credit_limit_paise))
    return await _propose(ctx, "set_credit_limit", {"customer_id": r.ref, "limit_paise": paise}, summary)


async def _set_hold(ctx: ToolContext, customer: str, on_hold: bool | str, reason: str | None = None):
    on_hold = str(on_hold).strip().lower() in {"true", "1", "yes"}  # models sometimes send booleans as strings
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        raise LedgerError(f"{r.name} abhi save nahi hua, hold nahi ho sakta.")
    c, lang = r.customer, ctx.merchant.language
    if on_hold and c.on_hold:
        raise LedgerError.of("err_already_on_hold", name=c.name)
    if not on_hold and not c.on_hold:
        raise LedgerError.of("err_not_on_hold", name=c.name)
    reason = " ".join((reason or "").split()) or None
    if on_hold:
        summary = t(lang, "sum_hold_place", name=c.name, reason=f" ({reason})" if reason else "")
    else:
        summary = t(lang, "sum_hold_lift", name=c.name)
        overdue = (await ledger.get_balance(ctx.session, ctx.merchant.id, c.id, ctx.today))["overdue_paise"]
        if overdue:
            summary += t(lang, "sum_hold_lift_overdue", amount=format_rupees(overdue))
    return await _propose(ctx, "set_hold", {"customer_id": r.ref, "on_hold": on_hold, "reason": reason}, summary)


async def _nudge_payment(ctx: ToolContext, c: Customer, payment_id: str | None) -> LedgerEntry:
    """The payment the nudge follows: the given one, or the customer's latest. Must be live and theirs."""
    live = [e for e in effective_entries(await ledger.customer_entries(ctx.session, c.id)) if e.type == EntryType.PAYMENT]
    if payment_id:
        pid = _parse_uuid(payment_id, "payment_id")
        match = [e for e in live if e.id == pid]
        if not match:
            raise LedgerError(f"Ye {c.name} ka koi chalu payment nahi hai; payment_id chhod dijiye.")
        return match[0]
    if not live:
        raise LedgerError(f"{c.name} ka koi payment nahi mila.")
    return max(live, key=lambda e: as_utc(e.created_at))


async def _send_partial_nudge(ctx: ToolContext, customer: str, payment_id: str | None = None):
    if not ctx.merchant.upi_vpa:
        raise LedgerError.of("err_reminder_needs_upi")
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        raise LedgerError(f"{r.name} abhi save nahi hua.")
    c = r.customer
    payment = await _nudge_payment(ctx, c, payment_id)
    if await nudge_sent(ctx.session, payment.id, ctx.today):
        raise LedgerError.of("err_nudge_sent_today", name=c.name)
    balance = (await ledger.get_balance(ctx.session, ctx.merchant.id, c.id, ctx.today))["balance_paise"]
    if balance <= 0:
        raise LedgerError.of("err_nothing_due", name=c.name)
    summary = t(ctx.merchant.language, "sum_nudge", name=c.name, amount=format_rupees(balance))
    return await _propose(ctx, "send_partial_nudge", {"customer_id": r.ref, "payment_id": str(payment.id)}, summary)


async def _check_udhaar_risk(ctx: ToolContext, customer: str | None = None, vpa: str | None = None,
                             recent_payment_rupees=None, name: str | None = None, amount_rupees=None) -> dict:
    c = None
    if customer:
        r = await _resolve_customer(ctx, customer)
        c = r.customer
        if c is None:
            raise LedgerError(f"{r.name} abhi save nahi hua; uski koi history nahi hai.")
    if recent_payment_rupees and not vpa:
        vpa = await trust_score.recent_payment_vpa(ctx.session, ctx.merchant.id, _amount_paise(recent_payment_rupees),
                                               ctx.today)
        if vpa is None:
            raise LedgerError("Aaj ya kal us amount ka koi UPI payment nahi mila. UPI ID poochiye.")
    if c is None and not vpa:
        raise LedgerError("Kiska check karna hai? Customer ka naam, UPI ID, ya haal ka payment batayiye.")
    if vpa:
        vpa = vpa.lower().replace(" at ", "@")  # assess() strips the rest; same rule as link_upi
    a = await trust_score.assess(ctx.session, ctx.merchant, ctx.today, customer=c, vpa=vpa, given_name=name)
    return trust_score.risk_card(a, _amount_paise(amount_rupees) if amount_rupees else None)


async def _resolve_family(ctx: ToolContext, raw: str):
    from app.family import service as family

    raw = " ".join(str(raw).split())
    if _is_uuid(raw):
        return await family.get_family(ctx.session, ctx.merchant.id, uuid.UUID(raw))
    found = await family.find_families(ctx.session, ctx.merchant.id, raw)
    if found["decision"] == "match":
        return found["matches"][0]["family"]
    if found["decision"] == "ambiguous":
        opts = " ya ".join(family._family_option(m["family"], m["head"]) for m in found["matches"])
        raise LedgerError(f"Kai parivar hain: {opts}? Kaun sa?")
    raise LedgerError(f"'{raw}' naam ka koi parivar nahi mila. Naya parivar banaun?")


async def _resolve_party(ctx: ToolContext, person_name=None, anchor_name=None, relation=None, family_hint=None):
    from app.family import service as family

    res = await family.resolve_party(ctx.session, ctx.merchant.id, person_name, anchor_name, relation, family_hint)
    if res["member_id"] and anchor_name and relation:  # learned as a nickname once the entry is confirmed
        ctx.spoken_party[res["member_id"]] = f"{anchor_name} ke {relation}"
    return res


async def _find_family(ctx: ToolContext, name: str):
    from app.family import service as family

    found = await family.find_families(ctx.session, ctx.merchant.id, name)
    return {"decision": found["decision"],
            "families": [{"family_id": str(m["family"].id), "name": m["family"].display_name, "head": m["head"].name,
                          "area_hint": m["family"].area_hint, "score": m["score"]} for m in found["matches"]]}


async def _create_family(ctx: ToolContext, name: str, head: str, area_hint: str | None = None, limit_rupees=None):
    r = await _resolve_customer(ctx, head)
    if r.customer is None:
        raise LedgerError(f"{r.name} abhi save nahi hua; pehle khata kholiye.")
    if r.customer.family_id:
        raise LedgerError.of("err_already_in_family", name=r.name)
    limit = rupees_to_paise(float(limit_rupees)) if limit_rupees else None
    summary = t(ctx.merchant.language, "sum_create_family", name=" ".join(name.split()), head=r.name)
    return await _propose(ctx, "create_family", {"name": name, "head_id": str(r.customer.id), "area_hint": area_hint,
                                                 "limit_paise": limit}, summary)


async def _add_member(ctx: ToolContext, family: str, name: str, relation: str | None = None, phone: str | None = None,
                      can_take_credit: bool | str = True, member_limit_rupees=None):
    from app.family.service import norm_relation

    f = await _resolve_family(ctx, family)
    existing = await ledger.find_customers(ctx.session, ctx.merchant.id, name)
    cid = None
    if existing["decision"] == "match" and existing["matches"][0]["customer"].family_id in (None, f.id):
        cid = str(existing["matches"][0]["customer"].id)
    allowed = str(can_take_credit).strip().lower() not in {"false", "0", "no"}
    summary = t(ctx.merchant.language, "sum_add_member", name=name, relation=norm_relation(relation) or "member",
                family=f.display_name)
    if not allowed:
        summary += t(ctx.merchant.language, "sum_no_credit")
    return await _propose(ctx, "add_member", {
        "family_id": str(f.id), "name": name, "relation": relation, "phone": phone, "customer_id": cid,
        "can_take_credit": allowed,
        "member_limit_paise": rupees_to_paise(float(member_limit_rupees)) if member_limit_rupees else None}, summary)


async def _remove_member(ctx: ToolContext, member: str):
    r = await _resolve_customer(ctx, member)
    if r.customer is None or r.customer.family_id is None:
        raise LedgerError(f"{r.name} kisi parivar mein nahi hai.")
    return await _propose(ctx, "remove_member", {"member_id": str(r.customer.id)},
                          t(ctx.merchant.language, "sum_remove_member", name=r.name))


async def _get_family_balance(ctx: ToolContext, family: str):
    from app.family import service as fam

    f = await _resolve_family(ctx, family)
    return await fam.family_balance(ctx.session, ctx.merchant.id, f.id)


async def _merge_families(ctx: ToolContext, family: str, into_family: str):
    src, dst = await _resolve_family(ctx, family), await _resolve_family(ctx, into_family)
    return await _propose(ctx, "merge_families", {"from_id": str(src.id), "into_id": str(dst.id)},
                          t(ctx.merchant.language, "sum_merge", src=src.display_name, dst=dst.display_name))


def credit_card(c: Customer) -> dict:
    st = ledger.credit_status(c)
    return {
        "limit": format_rupees(st["limit_paise"]) if st["limit_paise"] is not None else None,
        "limit_paise": st["limit_paise"],
        "percent_used": st["used_pct"],
        "near_limit": st["near_limit"],
    }


async def _get_credit_status(ctx: ToolContext, customer: str) -> dict:
    r = await _resolve_customer(ctx, customer)
    if r.customer is None:
        raise LedgerError(f"{r.name} abhi save nahi hua, unki koi limit nahi hai.")
    st = ledger.credit_status(r.customer)
    return {
        "name": r.name,
        "balance": format_rupees(st["balance_paise"]),
        "limit": format_rupees(st["limit_paise"]) if st["limit_paise"] is not None else None,
        "percent_used": st["used_pct"],
        "over_limit_by": format_rupees(st["over_by_paise"]) if st["over_by_paise"] else None,
        "no_new_udhaar": st["limit_paise"] == 0,
    }


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
    "set_credit_limit": _set_credit_limit,
    "get_credit_status": _get_credit_status,
    "set_hold": _set_hold,
    "send_partial_nudge": _send_partial_nudge,
    "check_udhaar_risk": _check_udhaar_risk,
    "edit_entry": _edit_entry,
    "waive_amount": _waive_amount,
    "resolve_party": _resolve_party,
    "find_family": _find_family,
    "create_family": _create_family,
    "add_member": _add_member,
    "remove_member": _remove_member,
    "get_family_balance": _get_family_balance,
    "merge_families": _merge_families,
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
