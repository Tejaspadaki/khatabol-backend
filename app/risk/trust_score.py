"""Trust Score + UPI Udhaar Check (plan.md §22).

Transparent rules over repayment behaviour at this shop. Advice only: nothing in the ledger, hold or
limit paths reads it (tests/test_trust_score.py checks the imports). Uses behaviour only, never names,
phones or any personal attribute; the name is read solely for the Level 4 UPI name check.

Levels of evidence:
  1  this shop: the customer's khata (or the khata linked to the UPI ID)
  2  all of the merchant's shops: needs shops (§23), so not available yet
  3  payment relationship: how long and how often the UPI ID has paid this shop
  4  name check: the UPI ID's registered name vs the name the customer gives
  5  Paytm network: future (plan.md §22), not built
"""

import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Customer, EntryType, LedgerEntry, Merchant, UpiTransaction, as_utc
from app.i18n import t
from app.ledger import service as ledger
from app.ledger.allocation import effective_entries, net_pairs, open_credits, overdue_amount
from app.nlp.hinglish_numbers import format_rupees
from app.nlp.names import normalize_name

DEFAULT_TERM_DAYS = 30        # udhaar without a due date is judged against this
NEW_START_PAISE = 30_000      # "Naya customer hai, ₹300 tak se shuru karein"
NEW_WITH_PAYMENTS_PAISE = 50_000
KNOWN_PAYER_PAISE = 100_000   # no khata yet, but a long UPI relationship
STRONG_UPI = (10, 6)          # at least 10 payments over at least 6 months
NAME_MATCH = 80               # rapidfuzz token-set ratio


@dataclass
class Assessment:
    level: str                    # high | medium | low | new
    suggested_paise: int
    reasons: list[str]
    advice: str
    checked: list[str]            # this_shop, upi_relationship, name_check
    customer_id: uuid.UUID | None = None
    vpa: str | None = None
    signals: dict = field(default_factory=dict)


def _day(dt: datetime) -> date:
    return as_utc(dt).astimezone(get_settings().tz).date()


def repayment_record(entries: list[LedgerEntry], allocations, today: date) -> dict:
    """When was each udhaar cleared, from `payment_allocations` (plan.md §20)? A payment counts from the
    later of its own day and the udhaar's day (an advance pays a new udhaar the day it is given).
    On time = cleared on or before its due date. A late udhaar that was at least half paid by its due date
    counts as half on time. Waived amounts are neither repaid nor late. Udhaar not yet due is not counted."""
    by_id = {e.id: e for e in entries}
    pairs = net_pairs(allocations)
    records = []
    for e in sorted(effective_entries(entries), key=lambda e: as_utc(e.created_at)):
        if e.type != EntryType.CREDIT:
            continue
        given = _day(e.created_at)
        rec = {"amount": e.amount_paise, "due": e.due_date or given + timedelta(days=DEFAULT_TERM_DAYS),
               "by_due": 0, "cleared": None}
        paid = sorted(((max(_day(by_id[pid].created_at), given), amt, by_id[pid].type)
                       for (pid, cid), amt in pairs.items() if cid == e.id), key=lambda x: x[0])
        waived = sum(amt for _, amt, kind in paid if kind == EntryType.ADJUSTMENT)
        rec["amount"] -= waived
        if rec["amount"] <= 0:
            continue
        total = 0
        for day, amt, kind in paid:
            if kind == EntryType.ADJUSTMENT:
                continue
            total += amt
            rec["by_due"] += amt if day <= rec["due"] else 0
            if total >= rec["amount"] and rec["cleared"] is None:
                rec["cleared"] = day
        records.append(rec)

    counted, on_time, late_days = 0, 0.0, []
    for rec in records:
        end = rec["cleared"] or (today if rec["due"] < today else None)
        if end is None:
            continue  # not due yet
        counted += 1
        if end <= rec["due"]:
            on_time += 1
            continue
        late_days.append((end - rec["due"]).days)
        if rec["by_due"] * 2 >= rec["amount"]:
            on_time += 0.5
    cleared = [r["amount"] for r in records if r["cleared"]]
    return {
        "counted": counted,
        "on_time": on_time,
        "avg_days_late": round(sum(late_days) / len(late_days)) if late_days else None,
        "largest_repaid": max(cleared) if cleared else 0,
        "median_credit": int(statistics.median([r["amount"] for r in records])) if records else 0,
    }


def _usual_balance(entries: list[LedgerEntry]) -> int | None:
    """Average running balance over the history, before the latest entry."""
    running, points = 0, []
    for e in sorted(effective_entries(entries), key=lambda e: as_utc(e.created_at)):
        running += e.amount_paise if e.type == EntryType.CREDIT else -e.amount_paise
        points.append(running)
    return int(sum(points[:-1]) / len(points[:-1])) if len(points) > 1 else None


async def _payments_from(session: AsyncSession, merchant_id: uuid.UUID, vpas: list[str]) -> list[UpiTransaction]:
    if not vpas:
        return []
    stmt = select(UpiTransaction).where(UpiTransaction.merchant_id == merchant_id, UpiTransaction.payer_vpa.in_(vpas))
    return list((await session.execute(stmt)).scalars())


def _name_matches(registered: str, given: str) -> bool:
    return fuzz.token_set_ratio(normalize_name(registered), normalize_name(given)) >= NAME_MATCH


def _round50(paise: int) -> int:
    return max(0, paise // 5000 * 5000)


async def assess(
    session: AsyncSession, merchant: Merchant, today: date, customer: Customer | None = None,
    vpa: str | None = None, given_name: str | None = None,
) -> Assessment:
    lang = merchant.language
    vpa = vpa.strip().lower().replace(" ", "") if vpa else None
    if customer is None and vpa:
        customer = await ledger.customer_for_vpa(session, merchant.id, vpa)
    checked, reasons, points = [], [], 0

    # Level 1: this shop's khata.
    rec = {"counted": 0, "on_time": 0.0, "avg_days_late": None, "largest_repaid": 0, "median_credit": 0}
    overdue = balance = tenure_days = 0
    vpas = [vpa] if vpa else []
    if customer is not None:
        checked.append("this_shop")
        entries, allocs = await ledger.credit_book(session, customer.id)
        rec = repayment_record(entries, allocs, today)
        balance = customer.balance_paise or 0
        overdue = overdue_amount(open_credits(entries, allocs), today)
        first = min([_day(e.created_at) for e in entries] + [_day(customer.created_at)])
        tenure_days = (today - first).days
        vpas = sorted(set(vpas) | set(await ledger.upi_ids(session, customer.id)))
        if rec["counted"]:
            r = rec["on_time"] / rec["counted"]
            points += 2 if r >= 0.9 else 1 if r >= 0.7 else -1 if r >= 0.4 else -2
            reasons.append(t(lang, "r_on_time", n=rec["counted"], k=f"{rec['on_time']:g}"))
        if rec["avg_days_late"]:
            points -= 2 if rec["avg_days_late"] > 15 else 1 if rec["avg_days_late"] > 3 else 0
            reasons.append(t(lang, "r_late", days=rec["avg_days_late"]))
        if overdue:
            points -= 1
            reasons.append(t(lang, "r_overdue", amount=format_rupees(overdue)))
        usual = _usual_balance(entries)
        if usual and usual > 0 and balance > 1.5 * usual:
            points -= 1
            reasons.append(t(lang, "r_balance_high", amount=format_rupees(balance), usual=format_rupees(usual)))
        if tenure_days >= 180:
            points += 1
            reasons.append(t(lang, "r_tenure", months=tenure_days // 30))

    # Level 3: how long and how often this UPI ID has paid the shop.
    payments = await _payments_from(session, merchant.id, vpas)
    upi_n = len(payments)
    upi_months = (today - min(_day(p.paid_at) for p in payments)).days // 30 if payments else 0
    strong_upi = upi_n >= STRONG_UPI[0] and upi_months >= STRONG_UPI[1]
    if vpas:
        checked.append("upi_relationship")
    if upi_n:
        reasons.append(t(lang, "r_upi", months=upi_months, n=upi_n))
        points += 1 if strong_upi else 0

    # Level 4: the UPI ID's registered name (payer name on its payments) vs the name given.
    name_match = None
    registered = next((p.payer_name for p in sorted(payments, key=lambda p: as_utc(p.paid_at), reverse=True)
                       if p.payer_name), None)
    if given_name and registered:
        checked.append("name_check")
        name_match = _name_matches(registered, given_name)
        if name_match:
            reasons.append(t(lang, "r_name_match"))
        else:
            points -= 2
            reasons.append(t(lang, "r_name_mismatch", upi=registered, given=given_name))

    # Level and suggested amount.
    if rec["counted"]:
        level = "high" if points >= 2 else "medium" if points >= 0 else "low"
        suggested = {
            "high": max(2 * rec["largest_repaid"], 2 * rec["median_credit"], NEW_START_PAISE),
            "medium": max(rec["largest_repaid"], rec["median_credit"], NEW_START_PAISE),
            "low": rec["median_credit"] // 2,
        }[level]
    else:
        reasons.insert(0, t(lang, "r_no_history"))
        if name_match is False:
            level, suggested = "low", 0
        elif strong_upi:
            level, suggested = "medium", KNOWN_PAYER_PAISE
        else:
            level, suggested = "new", NEW_WITH_PAYMENTS_PAISE if upi_n >= 3 else NEW_START_PAISE
    if customer is not None and customer.credit_limit_paise is not None:
        suggested = min(suggested, max(0, customer.credit_limit_paise - balance))
    suggested = _round50(suggested)

    if level == "new":
        advice = t(lang, "advice_new", amount=format_rupees(suggested))
    elif suggested == 0:
        advice = t(lang, "advice_none", level=level.title())
    else:
        advice = t(lang, f"advice_{level}", amount=format_rupees(suggested))
    return Assessment(
        level=level, suggested_paise=suggested, reasons=reasons, advice=advice, checked=checked,
        customer_id=customer.id if customer else None, vpa=vpa,
        signals={**rec, "overdue_paise": overdue, "balance_paise": balance, "tenure_days": tenure_days,
                 "upi_payments": upi_n, "upi_months": upi_months, "name_match": name_match, "points": points},
    )


def risk_card(a: Assessment, amount_paise: int | None = None) -> dict:
    card = {
        "level": a.level,
        "suggested_limit": format_rupees(a.suggested_paise),
        "suggested_limit_paise": a.suggested_paise,
        "reasons": a.reasons,
        "advice": a.advice,
        "checked": a.checked,
        "customer_id": str(a.customer_id) if a.customer_id else None,
        "vpa": a.vpa,
    }
    if amount_paise is not None:
        card["amount_ok"] = amount_paise <= a.suggested_paise
    return card


async def recent_payment_vpa(session: AsyncSession, merchant_id: uuid.UUID, amount_paise: int, today: date) -> str | None:
    """'Jo abhi ₹50 aaye the': the latest UPI payment of that amount, today or yesterday (IST)."""
    since = datetime.combine(today - timedelta(days=1), time.min, tzinfo=get_settings().tz).astimezone(timezone.utc)
    stmt = (
        select(UpiTransaction.payer_vpa)
        .where(UpiTransaction.merchant_id == merchant_id, UpiTransaction.amount_paise == amount_paise,
               UpiTransaction.paid_at >= since)
        .order_by(UpiTransaction.paid_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
