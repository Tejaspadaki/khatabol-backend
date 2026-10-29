"""Short Hinglish phrases built from real numbers, so spoken balances never come from the LLM."""

from datetime import date

from app.nlp.hinglish_numbers import format_rupees

MONTHS_HI = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
WEEKDAYS_HI = ["somvar", "mangalvar", "budhvar", "guruvar", "shukravar", "shanivar", "ravivar"]


def spoken_date(d: date, today: date | None = None) -> str:
    if today:
        delta = (d - today).days
        if delta == 0:
            return "aaj"
        if delta == 1:
            return "kal"
        if delta == 2:
            return "parson"
        if 2 < delta < 7:
            return f"{WEEKDAYS_HI[d.weekday()]} {d.day} {MONTHS_HI[d.month - 1]}"
    return f"{d.day} {MONTHS_HI[d.month - 1]}"


def balance_phrase(balance_paise: int) -> str:
    if balance_paise > 0:
        return f"ab {format_rupees(balance_paise)} baaki"
    if balance_paise < 0:
        return f"{format_rupees(-balance_paise)} advance jama hai"
    return "poora hisaab saaf"
