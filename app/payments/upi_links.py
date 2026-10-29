import re
from urllib.parse import quote, urlencode

REF_PREFIX = "KHATA-"
_REF = re.compile(r"KHATA-([A-Z0-9]{5})", re.IGNORECASE)


def payment_note(ref_code: str) -> str:
    return f"{REF_PREFIX}{ref_code}"


def ref_from_note(note: str | None) -> str | None:
    """The `tn` we put in reminder links comes back on the payment: second matching signal."""
    if not note:
        return None
    m = _REF.search(note)
    return m.group(1).upper() if m else None


def upi_intent_link(merchant_vpa: str, merchant_name: str, amount_paise: int, ref_code: str) -> str:
    params = {
        "pa": merchant_vpa,
        "pn": merchant_name,
        "am": f"{amount_paise / 100:.2f}",
        "cu": "INR",
        "tn": payment_note(ref_code),
    }
    return "upi://pay?" + urlencode(params, quote_via=quote)
