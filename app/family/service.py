"""Family (Parivaar) Khata (plan.md §17). Udhaar is spoken against the family; any member's UPI payment
settles it. The family's khata is the head's: entries carry family_id and taken_by_customer_id, and the
ledger write path (service.add_credit) checks member permission and the member / family limits."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Customer,
    EntryType,
    Family,
    FamilyMerge,
    Merchant,
    PaymentMode,
    as_utc,
)
from app.ledger import service as ledger
from app.ledger.allocation import effective_entries
from app.ledger.errors import LedgerError, NotFoundError
from app.nlp.hinglish_numbers import format_rupees
from app.nlp.names import CandidateName, decide, normalize_name, score_candidates
from app.payments.upi_links import upi_intent_link

# Spoken forms -> one relation word (plan.md §17.2: beta, beti, bahu, patni, pati, bhai, maa ...).
RELATIONS = {
    "beta": "beta", "bete": "beta", "beta ji": "beta", "ladka": "beta", "son": "beta",
    "beti": "beti", "ladki": "beti", "bitiya": "beti", "daughter": "beti",
    "bahu": "bahu", "bahoo": "bahu",
    "patni": "patni", "biwi": "patni", "wife": "patni", "gharwali": "patni", "mrs": "patni",
    "pati": "pati", "husband": "pati", "gharwala": "pati",
    "bhai": "bhai", "bhaiya": "bhai", "brother": "bhai",
    "behen": "behen", "bahen": "behen", "didi": "behen", "sister": "behen",
    "maa": "maa", "mummy": "maa", "mother": "maa", "amma": "maa",
    "papa": "papa", "pita": "papa", "father": "papa", "babuji": "papa",
    "dada": "dada", "dadi": "dadi", "nana": "nana", "nani": "nani", "bhabhi": "bhabhi", "devar": "devar",
}


def norm_relation(raw: str | None) -> str | None:
    if not raw:
        return None
    key = " ".join(raw.lower().split()).removesuffix(" ka").removesuffix(" ki").removesuffix(" ke")
    return RELATIONS.get(key, key)


# ---------------------------------------------------------------- families and members

async def get_family(session: AsyncSession, merchant_id: uuid.UUID, family_id: uuid.UUID) -> Family:
    f = await session.get(Family, family_id)
    if f is None or f.merchant_id != merchant_id:
        raise NotFoundError.of("err_family_not_found")
    return f


async def members(session: AsyncSession, family: Family) -> list[Customer]:
    rows = list((await session.execute(select(Customer).where(Customer.family_id == family.id))).scalars())
    return sorted(rows, key=lambda c: (c.id != family.head_customer_id, as_utc(c.created_at)))


async def create_family(session: AsyncSession, merchant_id: uuid.UUID, name: str, head_customer_id: uuid.UUID,
                        area_hint: str | None = None, credit_limit_paise: int | None = None) -> Family:
    head = await ledger.get_customer(session, merchant_id, head_customer_id, lock=True)
    if head.family_id:
        raise LedgerError.of("err_already_in_family", name=head.name)
    name = " ".join(name.split())
    f = Family(merchant_id=merchant_id, display_name=name, name_normalized=normalize_name(name),
               head_customer_id=head.id, area_hint=area_hint, credit_limit_paise=credit_limit_paise)
    session.add(f)
    await session.flush()
    head.family_id, head.relation_to_head = f.id, "self"
    await ledger.record_event(session, head, "FAMILY_CREATED", {"family_id": str(f.id), "name": name})
    return f


async def add_member(session: AsyncSession, merchant_id: uuid.UUID, family_id: uuid.UUID, name: str | None,
                     relation: str | None, phone: str | None = None, customer_id: uuid.UUID | None = None,
                     can_take_credit: bool = True, member_limit_paise: int | None = None) -> Customer:
    """An existing customer joins, or a new one is created (members need no phone, §17.2)."""
    f = await get_family(session, merchant_id, family_id)
    if customer_id:
        c = await ledger.get_customer(session, merchant_id, customer_id, lock=True)
        if c.family_id and c.family_id != f.id:
            raise LedgerError.of("err_already_in_family", name=c.name)
    else:
        c = await ledger.create_customer(session, merchant_id, name or "", phone)
    c.family_id, c.relation_to_head = f.id, norm_relation(relation) or "member"
    c.can_take_credit, c.member_limit_paise = can_take_credit, member_limit_paise
    await ledger.record_event(session, c, "FAMILY_MEMBER_ADDED", {"family_id": str(f.id), "relation": c.relation_to_head})
    return c


async def remove_member(session: AsyncSession, merchant_id: uuid.UUID, member_id: uuid.UUID) -> Customer:
    """Member leaves (marriage, moves out): past entries stay with the family, future ones go to their own khata."""
    c = await ledger.get_customer(session, merchant_id, member_id, lock=True)
    if c.family_id is None:
        raise LedgerError.of("err_not_in_family", name=c.name)
    f = await get_family(session, merchant_id, c.family_id)
    if f.head_customer_id == c.id:
        raise LedgerError.of("err_head_cannot_leave", name=c.name)
    await ledger.record_event(session, c, "FAMILY_MEMBER_LEFT", {"family_id": str(f.id)})
    c.family_id, c.relation_to_head = None, None
    await session.flush()
    return c


async def list_families(session: AsyncSession, merchant_id: uuid.UUID) -> list[Family]:
    stmt = select(Family).where(Family.merchant_id == merchant_id, Family.merged_into_id.is_(None)).order_by(Family.display_name)
    return list((await session.execute(stmt)).scalars())


async def find_families(session: AsyncSession, merchant_id: uuid.UUID, query: str) -> dict:
    """Fuzzy over family names and head names ("Sharma parivar", "Sharma ji ka ghar")."""
    fams = await list_families(session, merchant_id)
    heads = {f.id: await session.get(Customer, f.head_customer_id) for f in fams}
    q = " ".join(w for w in query.split() if w.lower() not in {"parivar", "family", "ghar", "ka", "ki", "ke", "wale"})
    cands = [CandidateName(f.id, f.display_name.replace("parivar", "").replace("family", ""),
                           [heads[f.id].name, *(heads[f.id].nicknames or [])]) for f in fams]
    scored = score_candidates(q or query, cands)
    by_id = {f.id: f for f in fams}
    keep = [m for m in scored if m.score >= 0.6][:4]
    return {"decision": decide(scored) if keep else "no_match",
            "matches": [{"family": by_id[m.customer_id], "head": heads[m.customer_id], "score": m.score} for m in keep]}


def _family_option(f: Family, head: Customer) -> str:
    return f"{f.display_name} ({f.area_hint or 'head ' + head.name}; family_id {f.id})"


async def resolve_party(session: AsyncSession, merchant_id: uuid.UUID, person_name: str | None = None,
                        anchor_name: str | None = None, relation: str | None = None,
                        family_hint: str | None = None) -> dict:
    """Who is "who" in a sentence (§17.3). Raises a LedgerError worded as the question to ask."""
    rel = norm_relation(relation)
    result = {"family_id": None, "family": None, "member_id": None, "member": None, "confidence": 0.0, "candidates": []}

    async def _one_customer(raw: str) -> tuple[Customer, float]:
        found = await ledger.find_customers(session, merchant_id, raw)
        if found["decision"] == "match":
            return found["matches"][0]["customer"], found["matches"][0]["score"]
        if found["decision"] == "ambiguous":
            opts = "; ".join(f"{m['customer'].name} (customer_id {m['customer'].id})" for m in found["matches"])
            raise LedgerError(f"'{raw}' naam ke kai log hain: {opts}. Kaun sa?")
        raise LedgerError(f"'{raw}' naam ka koi customer nahi mila.")

    if anchor_name and rel:  # 1. anchor + relation: "Ramesh ji ke bete"
        anchor, score = await _one_customer(anchor_name)
        if anchor.family_id is None:
            raise LedgerError(f"{anchor.name} ka parivar khata nahi bana. Parivar banaun?")
        f = await get_family(session, merchant_id, anchor.family_id)
        hits = [m for m in await members(session, f) if m.relation_to_head == rel and m.id != anchor.id]
        if not hits:
            raise LedgerError(f"{f.display_name} mein '{relation}' ka naam nahi hai. Add karun? "
                              f"(add_member with family_id {f.id}, relation {rel})")
        if len(hits) > 1:
            opts = "; ".join(f"{m.name} (customer_id {m.id})" for m in hits)
            raise LedgerError(f"{f.display_name} mein kai {rel} hain: {opts}. Kaun sa?")
        return {**result, "family_id": str(f.id), "family": f.display_name, "member_id": str(hits[0].id),
                "member": hits[0].name, "confidence": round(score, 2)}
    if person_name:  # 2. a person: a member of a family, or an individual customer
        c, score = await _one_customer(person_name)
        f = await get_family(session, merchant_id, c.family_id) if c.family_id else None
        return {**result, "family_id": str(f.id) if f else None, "family": f.display_name if f else None,
                "member_id": str(c.id), "member": c.name, "confidence": round(score, 2)}
    if family_hint:  # 3. a family: "Sharma parivar mein 300 likho"
        found = await find_families(session, merchant_id, family_hint)
        if found["decision"] == "match":
            m = found["matches"][0]
            return {**result, "family_id": str(m["family"].id), "family": m["family"].display_name,
                    "confidence": round(m["score"], 2)}
        if found["decision"] == "ambiguous":
            opts = " ya ".join(_family_option(m["family"], m["head"]) for m in found["matches"])
            raise LedgerError(f"Do {found['matches'][0]['family'].display_name} hain: {opts}?")
        raise LedgerError(f"'{family_hint}' naam ka koi parivar nahi mila. Naya parivar banaun?")
    raise LedgerError("Kiska? Naam, rishta (jaise 'Ramesh ke bete') ya parivar ka naam bataiye.")


# ---------------------------------------------------------------- balance, family-level udhaar, pay link

async def family_balance(session: AsyncSession, merchant_id: uuid.UUID, family_id: uuid.UUID) -> dict:
    """Total plus "kisne kitna liya": what is still open per member (and family-level entries)."""
    f = await get_family(session, merchant_id, family_id)
    head = await ledger.get_customer(session, merchant_id, f.head_customer_id)
    open_ = await ledger.open_credits_for(session, head.id)
    owed: dict = {}
    for c in open_:
        owed[c.entry.taken_by_customer_id] = owed.get(c.entry.taken_by_customer_id, 0) + c.remaining_paise
    rows = [{"customer_id": str(m.id), "name": m.name, "relation": m.relation_to_head, "can_take_credit": m.can_take_credit,
             "member_limit": format_rupees(m.member_limit_paise) if m.member_limit_paise is not None else None,
             "owed": format_rupees(owed.get(m.id, 0)), "owed_paise": owed.get(m.id, 0), "has_phone": bool(m.phone)}
            for m in await members(session, f)]
    known = {uuid.UUID(r["customer_id"]) for r in rows}
    unassigned = sum(v for k, v in owed.items() if k not in known)  # family-level, or members who left
    return {"family_id": str(f.id), "name": f.display_name, "area_hint": f.area_hint, "head": head.name,
            "head_customer_id": str(head.id), "total": format_rupees(sum(owed.values())),
            "total_paise": sum(owed.values()), "balance_paise": head.balance_paise,
            "limit": format_rupees(f.credit_limit_paise) if f.credit_limit_paise is not None else None,
            "unassigned": format_rupees(unassigned), "unassigned_paise": unassigned, "members": rows}


async def family_credit(session: AsyncSession, merchant_id: uuid.UUID, family_id: uuid.UUID, amount_paise: int,
                        **kw):
    """Udhaar for the family when nobody says which member took it ("Sharma parivar mein 300 likho")."""
    f = await get_family(session, merchant_id, family_id)
    return await ledger.add_credit(session, merchant_id, f.head_customer_id, amount_paise, family_level=True, **kw)


async def family_pay_link(session: AsyncSession, merchant: Merchant, family_id: uuid.UUID) -> dict:
    """The dynamic QR's content: due amount pre-filled, family reference in the UPI note. Made on the
    spot for the screen; never printed or stored."""
    f = await get_family(session, merchant.id, family_id)
    head = await ledger.get_customer(session, merchant.id, f.head_customer_id)
    if not merchant.upi_vpa:
        raise LedgerError.of("err_reminder_needs_upi")
    due = max(head.balance_paise, 0)
    return {"family_id": str(f.id), "amount": format_rupees(due), "amount_paise": due,
            "upi_link": upi_intent_link(merchant.upi_vpa, merchant.name, due, head.ref_code) if due else None}


# ---------------------------------------------------------------- merge duplicates (§17.4)

async def merge_families(session: AsyncSession, merchant_id: uuid.UUID, from_id: uuid.UUID, into_id: uuid.UUID,
                         merged_by: str) -> FamilyMerge:
    """Members, entries and UPI IDs move over; the ledger stays append-only (reversal + new entry)."""
    if from_id == into_id:
        raise LedgerError("Ek hi parivar ko khud mein merge nahi kar sakte.")
    src, dst = await get_family(session, merchant_id, from_id), await get_family(session, merchant_id, into_id)
    src_head = await ledger.get_customer(session, merchant_id, src.head_customer_id, lock=True)
    moved_members, upi = [], []
    for m in await members(session, src):
        m.family_id = dst.id
        if m.relation_to_head == "self":
            m.relation_to_head = "member"
        moved_members.append(str(m.id))
        upi += await ledger.upi_ids(session, m.id)
    await session.flush()
    moved_entries = []
    rows = await ledger.customer_entries(session, src_head.id)
    for e in sorted(effective_entries(rows), key=lambda e: as_utc(e.created_at)):
        await ledger.reverse_entry(session, merchant_id, e.id, note=f"family merge into {dst.display_name}", source=e.source)
        taken = e.taken_by_customer_id or src_head.id
        if e.type == EntryType.CREDIT:
            new = await ledger.add_credit(session, merchant_id, dst.head_customer_id, e.amount_paise, e.due_date, e.items,
                                          e.note, e.source, allow_on_hold=True, taken_by_customer_id=taken,
                                          allow_member_over_paise=10**12, allow_family_over_paise=10**12,
                                          allow_over_limit_paise=10**12)
        elif e.type == EntryType.PAYMENT:
            new = await ledger.record_payment(session, merchant_id, dst.head_customer_id, e.amount_paise,
                                              e.payment_mode or PaymentMode.CASH, e.source, note=e.note)
        else:
            new = await ledger.record_adjustment(session, merchant_id, dst.head_customer_id, e.amount_paise,
                                                 e.reason or "merge", source=e.source)
        moved_entries.append([str(e.id), str(new.id)])
    src.merged_into_id = dst.id
    log = FamilyMerge(merchant_id=merchant_id, from_family_id=src.id, into_family_id=dst.id, merged_by=merged_by,
                      moved={"members": moved_members, "entries": moved_entries, "upi_ids": upi})
    session.add(log)
    await ledger.record_event(session, src_head, "FAMILY_MERGED", {"from": str(src.id), "into": str(dst.id)})
    await session.flush()
    return log
