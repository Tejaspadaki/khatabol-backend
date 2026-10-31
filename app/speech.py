"""Short phrases built from real numbers, so spoken balances never come from the LLM."""

from datetime import date

from app.i18n import MONTHS, WEEKDAYS, lang_of, t
from app.nlp.hinglish_numbers import format_rupees

MONTHS_HI = MONTHS
WEEKDAYS_HI = WEEKDAYS["hi"]


def spoken_date(d: date, today: date | None = None, lang: str = "hi") -> str:
    if today:
        delta = (d - today).days
        if delta == 0:
            return t(lang, "today")
        if delta == 1:
            return t(lang, "tomorrow")
        if delta == 2:
            return t(lang, "day_after")
        if 2 < delta < 7:
            return f"{WEEKDAYS[lang_of(lang)][d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
    return f"{d.day} {MONTHS[d.month - 1]}"


def balance_phrase(balance_paise: int, lang: str = "hi") -> str:
    if balance_paise > 0:
        return t(lang, "balance_due", amount=format_rupees(balance_paise))
    if balance_paise < 0:
        return t(lang, "balance_advance", amount=format_rupees(-balance_paise))
    return t(lang, "balance_clear")
