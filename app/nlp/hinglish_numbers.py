"""Deterministic Hinglish amount normalizer.

Rewrites spoken amounts into digits before the LLM sees the text:
"saadhe teen sau" -> "350", "dedh hazaar" -> "1500", "1.5k" -> "1500".
"""

import re
from dataclasses import dataclass

NUMBER_WORDS: dict[str, int] = {
    # Latin spellings as Indian STT tends to emit them
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4,
    "paanch": 5, "panch": 5, "paanc": 5, "chhe": 6, "chhah": 6, "chah": 6, "che": 6,
    "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9, "das": 10, "dus": 10,
    "gyarah": 11, "gyara": 11, "barah": 12, "bara": 12, "terah": 13, "tera": 13,
    "chaudah": 14, "chauda": 14, "pandrah": 15, "pandra": 15, "solah": 16, "sola": 16,
    "satrah": 17, "satra": 17, "atharah": 18, "athara": 18, "unnis": 19, "unees": 19,
    "bees": 20, "bis": 20, "ikkis": 21, "bais": 22, "baais": 22, "teis": 23, "tees": 30,
    "chaubis": 24, "pachees": 25, "pachchis": 25, "pacchis": 25, "chhabbis": 26,
    "tis": 30, "paintis": 35, "paintees": 35, "chalis": 40, "chaalis": 40,
    "pachaas": 50, "pachas": 50, "pachpan": 55, "saath": 60, "sath": 60,
    "pachattar": 75, "sattar": 70, "assi": 80, "nabbe": 90, "nabbey": 90,
    # Devanagari
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6, "छः": 6,
    "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "बीस": 20, "पच्चीस": 25, "तीस": 30,
    "चालीस": 40, "पचास": 50, "साठ": 60, "सत्तर": 70, "अस्सी": 80, "नब्बे": 90,
}

MULTIPLIERS: dict[str, int] = {
    "sau": 100, "sow": 100, "सौ": 100, "hundred": 100,
    "hazaar": 1000, "hazar": 1000, "hajar": 1000, "hajaar": 1000, "hazzar": 1000,
    "हज़ार": 1000, "हजार": 1000, "thousand": 1000, "k": 1000,
    "lakh": 100_000, "lac": 100_000, "laakh": 100_000, "लाख": 100_000,
}

# "dedh hazaar" = 1.5 x 1000, "dhai sau" = 2.5 x 100
FRACTION_WORDS: dict[str, float] = {
    "dedh": 1.5, "derh": 1.5, "डेढ़": 1.5, "डेढ": 1.5,
    "dhai": 2.5, "dhaai": 2.5, "adhai": 2.5, "ढाई": 2.5,
}

# "sawa do sau" = (2 + 0.25) x 100, "paune do sau" = (2 - 0.25) x 100
MODIFIERS: dict[str, float] = {
    "sawa": 0.25, "savva": 0.25, "sava": 0.25, "सवा": 0.25,
    "saadhe": 0.5, "sadhe": 0.5, "saade": 0.5, "sade": 0.5, "साढ़े": 0.5, "साढे": 0.5,
    "paune": -0.25, "pone": -0.25, "paone": -0.25, "पौने": -0.25,
}

# Number words that are also everyday Hindi words ("bhej do", "ek customer", "saath mein").
AMBIGUOUS = {"do", "ek", "sat", "saat", "sath", "saath", "che", "tin", "bis", "tera", "bara", "das", "दो", "एक"}

CURRENCY_UNITS = {"rupaye", "rupay", "rupee", "rupees", "rupiya", "rupiye", "rs", "rs.", "₹", "रुपये", "रुपए"}
TIME_UNITS = {"din", "dino", "hafte", "hafta", "mahine", "mahina", "tareekh", "tarikh", "taarikh", "date", "baje"}

_DIGITS = re.compile(r"^\d+(?:\.\d+)?$")
_K_SUFFIX = re.compile(r"^(\d+(?:\.\d+)?)k$")
_PUNCT = ",.?!;:"


@dataclass
class Amount:
    value: int
    text: str  # the original span


def _clean(tok: str) -> str:
    return tok.lower().strip(_PUNCT)


def _num(tok: str) -> float | None:
    if tok in NUMBER_WORDS:
        return NUMBER_WORDS[tok]
    if _DIGITS.match(tok):
        return float(tok)
    return None


def _parse_group(toks: list[str], i: int) -> tuple[float, int, int | None] | None:
    """One "<modifier?> <base> <multiplier?>" group -> (value, next_index, multiplier)."""
    if i >= len(toks):
        return None
    t = toks[i]
    nxt = toks[i + 1] if i + 1 < len(toks) else None

    if t in FRACTION_WORDS:
        if nxt in MULTIPLIERS:
            return FRACTION_WORDS[t] * MULTIPLIERS[nxt], i + 2, MULTIPLIERS[nxt]
        return None

    delta = 0.0
    if t in MODIFIERS:
        delta = MODIFIERS[t]
        if nxt in MULTIPLIERS:  # "sawa sau" = 125
            return (1 + delta) * MULTIPLIERS[nxt], i + 2, MULTIPLIERS[nxt]
        i += 1
        if i >= len(toks):
            return None
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None

    base = _num(t)
    if base is None:
        return None
    if nxt in MULTIPLIERS:
        return (base + delta) * MULTIPLIERS[nxt], i + 2, MULTIPLIERS[nxt]
    if delta:
        return None
    return base, i + 1, None


def _parse_phrase(toks: list[str], i: int) -> tuple[float, int, bool] | None:
    total = 0.0
    j = i
    last_mult = float("inf")
    saw_mult = False
    while j < len(toks):
        g = _parse_group(toks, j)
        if g is None:
            break
        value, nj, mult = g
        if mult is not None:
            if mult >= last_mult:
                break
            last_mult = mult
            total += value
            j = nj
            saw_mult = True
            continue
        # A bare trailing number: "paanch sau pachaas". Not "do hazaar 5 tareekh".
        if saw_mult:
            after = toks[nj] if nj < len(toks) else None
            if value >= last_mult or after in TIME_UNITS:
                break
        total += value
        j = nj
        break
    if j == i:
        return None
    return total, j, saw_mult


def normalize_amounts(text: str) -> tuple[str, list[Amount]]:
    """Return (text with spoken amounts as digits, amounts found)."""
    raw = text.split()
    toks = []
    for tok in raw:
        c = _clean(tok)
        m = _K_SUFFIX.match(c)
        toks.append(str(float(m.group(1)) * 1000) if m else c.lstrip("₹"))

    out: list[str] = []
    amounts: list[Amount] = []
    i = 0
    while i < len(toks):
        parsed = _parse_phrase(toks, i)
        if parsed is None:
            out.append(raw[i])
            i += 1
            continue
        value, j, saw_mult = parsed
        span = raw[i:j]
        nxt = toks[j] if j < len(toks) else None
        single_word = j - i == 1 and not saw_mult
        if single_word and toks[i] in AMBIGUOUS and nxt not in CURRENCY_UNITS | TIME_UNITS:
            out.append(raw[i])
            i += 1
            continue
        if value != int(value):  # "saadhe teen" without a multiplier is not an amount
            out.extend(span)
            i = j
            continue
        trailing = raw[j - 1][len(raw[j - 1].rstrip(_PUNCT)):]
        out.append(f"{int(value)}{trailing}")
        is_amount = (
            saw_mult
            or nxt in CURRENCY_UNITS
            or raw[i].startswith("₹")
            or _K_SUFFIX.match(_clean(raw[i]))
            or (_DIGITS.match(toks[i]) and value >= 10)
        )
        if is_amount and nxt not in TIME_UNITS:
            amounts.append(Amount(int(value), " ".join(span)))
        i = j
    return " ".join(out), amounts


def parse_amount(text: str) -> int | None:
    """Best single rupee amount in an utterance, or None."""
    _, amounts = normalize_amounts(text)
    if amounts:
        return amounts[0].value
    digits = [int(float(t)) for t in re.findall(r"\d+(?:\.\d+)?", text)]
    return digits[0] if len(digits) == 1 else None


def rupees_to_paise(rupees: float) -> int:
    paise = round(rupees * 100)
    return int(paise)


def format_rupees(paise: int) -> str:
    """Indian digit grouping: 123456700 paise -> '₹12,34,567'."""
    neg = paise < 0
    rupees, rem = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    if rem:
        s += f".{rem:02d}"
    return ("-" if neg else "") + "₹" + s
