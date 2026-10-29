from datetime import date

import pytest

from app.nlp.confirm import classify_confirmation
from app.nlp.dates import resolve_due_date
from app.nlp.hinglish_numbers import format_rupees, normalize_amounts, parse_amount
from app.nlp.names import CandidateName, decide, normalize_name, score_candidates


@pytest.mark.parametrize("spoken,value", [
    ("dhai sau", 250), ("dedh hazaar", 1500), ("sawa sau", 125), ("saadhe teen sau", 350),
    ("paune do sau", 175), ("paanch sau pachaas", 550), ("1.5k", 1500), ("do hazaar paanch sau", 2500),
    ("ek lakh bees hazaar", 120000), ("डेढ़ हज़ार", 1500), ("₹750", 750), ("do sau rupaye", 200),
    ("Ramesh ko 500 ka saamaan", 500),
])
def test_amounts(spoken, value):
    assert parse_amount(spoken) == value


def test_amount_rewrites_text_but_keeps_everyday_words():
    assert normalize_amounts("Ramesh ko saadhe teen sau ka saamaan, somvar tak")[0] == "Ramesh ko 350 ka saamaan, somvar tak"
    assert normalize_amounts("sabko reminder bhej do")[0] == "sabko reminder bhej do"
    assert normalize_amounts("ek customer aaya")[0] == "ek customer aaya"
    # the due-date number must not merge into the amount
    assert normalize_amounts("do hazaar 5 tareekh tak")[0] == "2000 5 tareekh tak"


TODAY = date(2026, 10, 29)  # a Thursday


@pytest.mark.parametrize("spoken,expected", [
    ("5 tareekh", date(2026, 11, 5)),
    ("30 tareekh", date(2026, 10, 30)),
    ("29 tareekh", date(2026, 11, 29)),  # today's date number means next month
    ("agle hafte", date(2026, 11, 5)),
    ("somvar ko", date(2026, 11, 2)),
    ("guruvar", date(2026, 11, 5)),  # same weekday as today -> next week
    ("salary ke baad", date(2026, 11, 1)),
    ("kal", date(2026, 10, 30)),
    ("parson", date(2026, 10, 31)),
    ("do din mein", date(2026, 10, 31)),
    ("teen hafte", date(2026, 11, 19)),
    ("mahine ki 1 tareekh", date(2026, 11, 1)),
    ("agle mahine 10 tareekh", date(2026, 11, 10)),
    ("5 November", date(2026, 11, 5)),
    ("2026-12-01", date(2026, 12, 1)),
])
def test_dates(spoken, expected):
    assert resolve_due_date(spoken, TODAY) == expected


def test_unknown_date():
    assert resolve_due_date("jab paise honge", TODAY) is None


def test_month_end_clamps():
    assert resolve_due_date("31 tareekh", date(2026, 11, 2)) == date(2026, 12, 31)
    assert resolve_due_date("31 tareekh", date(2027, 1, 31)) == date(2027, 2, 28)


CUSTOMERS = [
    CandidateName(1, "Ramesh Patil"),
    CandidateName(2, "Ramesh Sharma"),
    CandidateName(3, "Sunita Devi", ["sunita didi"]),
    CandidateName(4, "Mohan Lal"),
]


def test_normalize_name():
    assert normalize_name("Ramesh Bhaiya ji") == "ramesh"
    assert normalize_name("Sunita didi ka") == "sunita"


@pytest.mark.parametrize("query,decision,top", [
    ("Ramesh Patil", "match", "Ramesh Patil"),
    ("ramesh", "ambiguous", None),
    ("Ramesh bhaiya", "ambiguous", None),
    ("सुनीता", "match", "Sunita Devi"),
    ("sunitha", "match", "Sunita Devi"),
    ("patil", "match", "Ramesh Patil"),
    ("Rmesh sharma", "match", "Ramesh Sharma"),
    ("suresh", "no_match", None),
])
def test_customer_resolution(query, decision, top):
    matches = score_candidates(query, CUSTOMERS)
    assert decide(matches) == decision
    if top:
        assert matches[0].name == top


@pytest.mark.parametrize("utterance,verdict", [
    ("haan", "yes"), ("haan ji sahi hai", "yes"), ("ok kar do", "yes"), ("हाँ", "yes"),
    ("nahi", "no"), ("cancel karo", "no"), ("nahi galat hai", "no"),
    ("500 nahi 600", None), ("haan aur Sunita ne 200 diye", None), ("Ramesh ka kitna baaki hai", None),
])
def test_confirmation(utterance, verdict):
    assert classify_confirmation(utterance) == verdict


def test_format_rupees():
    assert format_rupees(123456700) == "₹12,34,567"
    assert format_rupees(35050) == "₹350.50"
    assert format_rupees(-10000) == "-₹100"


@pytest.mark.parametrize("utterance,verdict", [
    ("bas", "cancel"), ("bas karo", "cancel"), ("band karo", "cancel"), ("dhanyavaad", "cancel"),
    ("thank you", "cancel"), ("aaj ke liye itna hi, bye", "cancel"), ("nahi bas", "cancel"),
    ("haan bas", "save"), ("theek hai bas", "save"), ("ok bye", "save"), ("बस", "cancel"),
    ("bas 200 hi diye", None), ("bas ramesh ka batao", None), ("haan", None), ("Ramesh ka kitna baaki hai", None),
])
def test_goodbye(utterance, verdict):
    from app.nlp.confirm import classify_goodbye

    assert classify_goodbye(utterance) == verdict
