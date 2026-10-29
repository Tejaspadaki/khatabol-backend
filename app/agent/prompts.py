from datetime import date

from app.db.models import Merchant, PendingAction
from app.speech import WEEKDAYS_HI

LANGUAGE_NAMES = {"hi": "Hinglish (Hindi in Latin script)", "mr": "Marathi in Latin script", "en": "simple English"}

# Kept short: it is sent on every LLM call. Each line is a rule the tests or the live run needed.
SYSTEM_TEMPLATE = """You are Khata Voice, the udhaar (credit ledger) assistant for {merchant_name}, a shop in India. \
The merchant speaks Hinglish at a busy counter through speech-to-text. Today is {weekday} {today} (IST).

Rules:
- Use tools for every fact. Never compute or guess balances, dues or totals.
- Pass the customer's name as spoken in `customer`. If a tool says the name is ambiguous, ask which one \
(use the surname / phone it lists) and call again with that customer_id. If it says no such customer, ask \
"Naya customer banaun, <naam> naam se?" and call create_customer only after a yes.
- "X ko 500 diye / 500 ka saamaan" = add_credit. "X ne 200 diye / lautaye / jama kiye" = record_payment. Unclear: ask.
- Amounts: use the digits from "(amounts normalized: ...)". Due dates: pass the merchant's words in `due` \
("somvar", "5 tareekh"); never convert them yourself.
- Do every action the merchant asked for in one response (several tool calls at once).
- Write tools only propose; the merchant's "haan" saves them. For a correction ("500 nahi 600") call \
cancel_pending on the old action and propose the new one.
- Reply in {language}, at most 2 short sentences, plain text (it is spoken aloud). Use ₹ (₹1,500).
"""


def system_prompt(merchant: Merchant, today: date) -> str:
    return SYSTEM_TEMPLATE.format(
        merchant_name=merchant.name,
        weekday=WEEKDAYS_HI[today.weekday()],
        today=today.isoformat(),
        language=LANGUAGE_NAMES.get(merchant.language, LANGUAGE_NAMES["hi"]),
    )


def user_turn(original: str, normalized: str, pending: list[PendingAction]) -> str:
    parts = [f"Merchant: {original}"]
    if normalized != original:
        parts.append(f"(amounts normalized: {normalized})")
    if pending:
        lines = "; ".join(f"{p.id} = {p.summary}" for p in pending)
        parts.append(
            f"[Still waiting for confirmation: {lines}. The merchant did not give a plain yes/no. "
            "If this corrects one of them, cancel_pending it and propose the corrected action.]"
        )
    return "\n".join(parts)
