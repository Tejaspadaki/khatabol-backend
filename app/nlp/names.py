"""Customer name normalization and fuzzy/phonetic matching (plan.md §4.3)."""

import re
from dataclasses import dataclass, field

from indic_transliteration import sanscript
from rapidfuzz import fuzz

HONORIFICS = {
    "bhaiya", "bhaiyya", "bhaia", "bhai", "bhaisaab", "bhaisahab", "ji", "jee", "didi", "di",
    "behen", "behenji", "uncle", "aunty", "auntie", "anty", "seth", "sethji", "saab", "sahab",
    "sir", "madam", "mr", "mrs", "shri", "shrimati", "smt", "kaka", "kaki", "chacha", "chachi",
    "mama", "mami", "dada", "dadi", "nana", "nani", "babu", "bhabhi", "anna", "akka", "tai",
    "भैया", "जी", "दीदी", "भाई", "सेठ",
}
# Words STT often glues onto the name: "ramesh ka", "ramesh ko", "ramesh ne"
PARTICLES = {"ka", "ki", "ke", "ko", "ne", "se", "wala", "wale", "wali", "waale"}

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_NON_ALPHA = re.compile(r"[^a-z ]+")

STRONG_MATCH = 0.85
MIN_CANDIDATE = 0.6
CLEAR_GAP = 0.08


def transliterate(text: str) -> str:
    if _DEVANAGARI.search(text):
        text = sanscript.transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
    return text


def normalize_name(name: str) -> str:
    """'Ramesh Bhaiya ji' / 'रमेश' -> 'ramesh'."""
    words = [w for w in name.split() if w.lower() not in HONORIFICS]
    text = transliterate(" ".join(words)).lower()
    text = _NON_ALPHA.sub(" ", text)
    words = [w for w in text.split() if w not in HONORIFICS and w not in PARTICLES]
    return " ".join(words)


_PHONETIC_RULES = [
    ("ksh", "x"), ("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"),
    ("ph", "f"), ("bh", "b"), ("dh", "d"), ("th", "t"), ("kh", "k"), ("gh", "g"),
    ("jh", "j"), ("chh", "c"), ("ch", "c"), ("sh", "s"), ("w", "v"), ("z", "j"),
    ("q", "k"), ("ck", "k"), ("y", "i"),
]


def _phonetic_word(word: str) -> str:
    for a, b in _PHONETIC_RULES:
        word = word.replace(a, b)
    word = re.sub(r"(.)\1+", r"\1", word)  # collapse doubles: "pappu" -> "papu"
    word = re.sub(r"(?<=[^aeiou])h", "", word)  # stray aspiration
    if len(word) > 3 and word.endswith("a"):  # ITRANS schwa: "ramesha" -> "rames"
        word = word[:-1]
    return word


def phonetic_key(normalized: str) -> str:
    """Indic-aware phonetic code: collapses aspirates, vowel length, v/w."""
    return " ".join(_phonetic_word(w) for w in normalized.split())


@dataclass
class CandidateName:
    customer_id: object
    name: str
    nicknames: list[str] = field(default_factory=list)


@dataclass
class ScoredMatch:
    customer_id: object
    name: str
    score: float
    matched_on: str


def _score_pair(q_norm: str, q_key: str, cand_norm: str) -> float:
    if not cand_norm:
        return 0.0
    if q_norm == cand_norm:
        return 1.0
    c_key = phonetic_key(cand_norm)
    if q_key == c_key:
        return 0.97
    score = max(fuzz.ratio(q_norm, cand_norm), fuzz.ratio(q_key, c_key)) / 100
    q_words, c_words = q_norm.split(), cand_norm.split()
    # "ramesh" vs "ramesh patil": the query is a prefix of the full name
    if len(q_words) < len(c_words):
        prefix = " ".join(c_words[: len(q_words)])
        if prefix == q_norm or phonetic_key(prefix) == q_key:
            score = max(score, 0.93)
        else:
            sub = fuzz.ratio(q_key, phonetic_key(prefix)) / 100
            score = max(score, sub * 0.9)
        # Surname-only query: "patil"
        if len(q_words) == 1 and q_words[0] in c_words[1:]:
            score = max(score, 0.88)
    return score


def score_candidates(query: str, candidates: list[CandidateName]) -> list[ScoredMatch]:
    q_norm = normalize_name(query)
    if not q_norm:
        return []
    q_key = phonetic_key(q_norm)
    results = []
    for c in candidates:
        best, on = _score_pair(q_norm, q_key, normalize_name(c.name)), "name"
        for nick in c.nicknames:
            s = _score_pair(q_norm, q_key, normalize_name(nick))
            if s > best:
                best, on = s, f"nickname:{nick}"
        results.append(ScoredMatch(c.customer_id, c.name, round(best, 3), on))
    results.sort(key=lambda m: m.score, reverse=True)
    return results


def decide(matches: list[ScoredMatch]) -> str:
    """'match' | 'ambiguous' | 'no_match'."""
    if not matches or matches[0].score < MIN_CANDIDATE:
        return "no_match"
    top = matches[0]
    runner_up = matches[1].score if len(matches) > 1 else 0.0
    if top.score >= STRONG_MATCH and top.score - runner_up >= CLEAR_GAP:
        return "match"
    return "ambiguous"
