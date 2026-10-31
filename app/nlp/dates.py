"""Resolve spoken Hinglish due dates ("somvar tak", "5 tareekh", "do din mein") to real dates."""

import calendar
import re
from datetime import date, timedelta

from app.nlp.hinglish_numbers import normalize_amounts

WEEKDAYS: dict[str, int] = {
    "somvar": 0, "som": 0, "monday": 0, "mon": 0, "सोमवार": 0,
    "mangalvar": 1, "mangal": 1, "tuesday": 1, "tue": 1, "मंगलवार": 1,
    "budhvar": 2, "budh": 2, "wednesday": 2, "wed": 2, "बुधवार": 2,
    "guruvar": 3, "veervar": 3, "brihaspativar": 3, "thursday": 3, "thu": 3, "गुरुवार": 3,
    "shukravar": 4, "shukra": 4, "friday": 4, "fri": 4, "शुक्रवार": 4,
    "shanivar": 5, "shani": 5, "saturday": 5, "sat": 5, "शनिवार": 5,
    "ravivar": 6, "itvar": 6, "etvar": 6, "sunday": 6, "sun": 6, "रविवार": 6,
}

MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "janvari": 1, "feb": 2, "february": 2, "farvari": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "mai": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "agast": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "aktubar": 10, "nov": 11, "november": 11,
    "navambar": 11, "dec": 12, "december": 12, "disambar": 12,
}

DAY_WORDS = {"din", "dino", "dinon", "days", "day"}
WEEK_WORDS = {"hafte", "hafta", "haftey", "week", "weeks"}
MONTH_WORDS = {"mahine", "mahina", "month", "months"}
DATE_WORDS = {"tareekh", "tarikh", "taarikh", "tarik", "date"}


def _clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _add_months(d: date, n: int, day: int | None = None) -> date:
    m = d.month - 1 + n
    return _clamp_day(d.year + m // 12, m % 12 + 1, day or d.day)


def _next_weekday(today: date, wd: int) -> date:
    ahead = (wd - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


def _next_day_of_month(today: date, day: int) -> date:
    if day > today.day and day <= calendar.monthrange(today.year, today.month)[1]:
        return date(today.year, today.month, day)
    return _add_months(today.replace(day=1), 1, day)


def resolve_due_date(phrase: str | None, today: date, salary_day: int = 1) -> date | None:
    """Deterministic resolution against `today` (IST). Returns None if not understood."""
    if not phrase:
        return None
    text = phrase.strip().lower()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    text, _ = normalize_amounts(text)
    toks = [t.strip(",.?!") for t in text.split()]
    if not toks:
        return None
    tokset = set(toks)

    if tokset & {"aaj", "today", "आज"}:
        return today
    if tokset & {"parson", "parso", "parsoon", "परसों", "parva", "परवा"}:
        return today + timedelta(days=2)
    if tokset & {"narson", "narso"}:
        return today + timedelta(days=3)
    if tokset & {"kal", "tomorrow", "कल", "udya", "उद्या"}:
        return today + timedelta(days=1)
    if "salary" in tokset or "tankhwah" in tokset or "pagaar" in tokset:
        return _add_months(today.replace(day=1), 1, salary_day)

    agla = bool(tokset & {"agle", "agla", "agli", "next", "अगले"})
    for i, tok in enumerate(toks):
        n = int(toks[i - 1]) if i > 0 and toks[i - 1].isdigit() else None
        if tok in DAY_WORDS and n is not None:
            return today + timedelta(days=n)
        if tok in WEEK_WORDS:
            return today + timedelta(days=7 * (n or 1))
        if tok in MONTH_WORDS:
            # "mahine ki 1 tareekh" / "agle mahine 5 tareekh" are handled by the date rule below
            if not tokset & DATE_WORDS:
                return _add_months(today, n or 1)

    for tok in toks:
        if tok in WEEKDAYS:
            return _next_weekday(today, WEEKDAYS[tok])

    # "5 November", "November 5", "5 nov"
    for i, tok in enumerate(toks):
        if tok in MONTHS:
            neighbours = [toks[j] for j in (i - 1, i + 1) if 0 <= j < len(toks)]
            day = next((int(x) for x in neighbours if x.isdigit() and 1 <= int(x) <= 31), None)
            if day:
                d = _clamp_day(today.year, MONTHS[tok], day)
                return d if d >= today else _clamp_day(today.year + 1, MONTHS[tok], day)

    # "5 tareekh", "mahine ki 1 tareekh", "15 ko"
    m = re.search(r"\b(\d{1,2})\s*(?:tareekh|tarikh|taarikh|tarik|date|ko|tak)\b", text)
    if m and 1 <= int(m.group(1)) <= 31:
        day = int(m.group(1))
        if agla and tokset & MONTH_WORDS:
            return _add_months(today.replace(day=1), 1, day)
        return _next_day_of_month(today, day)

    if agla:
        return today + timedelta(days=7)
    return None
