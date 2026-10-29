"""Deterministic yes/no detection for the confirmation gate. No LLM involved."""

import re

YES = {
    "haan", "han", "haa", "ha", "haanji", "hanji", "ji", "jee", "yes", "yeah", "yep", "ok", "okay",
    "theek", "thik", "sahi", "kar", "karo", "kardo", "save", "done", "confirm", "bilkul", "pakka",
    "chalega", "hmm", "hm", "acha", "accha", "achha", "हाँ", "हां", "जी", "ठीक", "सही", "बिल्कुल",
}
NO = {
    "nahi", "nahin", "nai", "na", "naa", "no", "nope", "mat", "cancel", "galat", "rehne", "ruko",
    "chhodo", "chodo", "नहीं", "मत", "गलत", "रहने",
}
FILLER = {"hai", "h", "do", "de", "dijiye", "please", "bhai", "bhaiya", "ho", "gaya", "yaar", "dena", "doh"}

_TOKEN = re.compile(r"[\wऀ-ॿ]+")


def classify_confirmation(text: str) -> str | None:
    """Return 'yes', 'no', or None when the utterance carries more than a confirmation.

    "500 nahi 600" -> None (it's a correction; the LLM handles it).
    """
    toks = [t.lower() for t in _TOKEN.findall(text)]
    if not toks or len(toks) > 5 or any(t.isdigit() for t in toks):
        return None
    meaningful = [t for t in toks if t not in FILLER]
    if not meaningful:
        return None
    if all(t in YES for t in meaningful):
        return "yes"
    if any(t in NO for t in meaningful) and all(t in NO | YES for t in meaningful):
        return "no"
    return None


# ---------------------------------------------------------------- ending a conversation

GOODBYE = {
    "bas", "band", "khatam", "khtm", "bye", "alvida", "dhanyavaad", "dhanyawad", "dhanyavad", "dhanyawaad",
    "shukriya", "thanks", "thank", "thankyou", "stop", "बस", "बंद", "खत्म", "धन्यवाद", "शुक्रिया",
}
# Words that go with a goodbye without changing its meaning ("bas karo", "aaj ke liye itna hi").
GOODBYE_EXTRA = {"you", "chalo", "abhi", "itna", "itni", "hi", "kar", "karo", "kardo", "ho", "gaya", "phir",
                 "milte", "hain", "baat", "aaj", "ke", "liye", "sab", "ki", "ka", "bhi", "tha"}
# Inside a goodbye, these mean "and save what's pending" ("haan bas", "theek hai, bas").
_SAVE_WORDS = YES - {"ji", "jee", "kar", "karo", "kardo", "hmm", "hm"}


def classify_goodbye(text: str) -> str | None:
    """Is the merchant ending the conversation? Returns None, 'save' (end and keep pending entries)
    or 'cancel' (end and drop them). "bas 200 hi diye" -> None (it has an amount)."""
    toks = [t.lower() for t in _TOKEN.findall(text)]
    if not toks or len(toks) > 8 or any(t.isdigit() for t in toks):
        return None
    if not any(t in GOODBYE for t in toks):
        return None
    if not all(t in GOODBYE | GOODBYE_EXTRA | YES | NO | FILLER for t in toks):
        return None
    if any(t in NO for t in toks):
        return "cancel"
    return "save" if any(t in _SAVE_WORDS for t in toks) else "cancel"
