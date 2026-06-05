"""
filter.py – Content filter for Fliphone.

Philosophy
----------
Censors slurs, hate speech, and harmful content. NOT general profanity.
Words like "fuck", "shit", "damn" pass through untouched.

How it works
------------
- Case-insensitive word boundary matching
- Leet-speak normalisation (3→e, 4→a, 1→i, 0→o, @→a, $→s)
- Matched words replaced with [censored]
- Returns (filtered_text, was_censored: bool)
- Custom words loaded from DB at runtime via load_custom_words()
"""

import re
from typing import Optional

# ── Hardcoded word list ───────────────────────────────────────────────────────
_BLOCKED: list[str] = [
    # Racial slurs
    "nigger", "nigga", "nigg3r", "n1gger", "n1gga",
    "chink", "ch1nk", "gook", "g00k",
    "spic", "sp1c", "wetback", "w3tback",
    "kike", "k1ke",
    "raghead", "sandnigger", "sand nigger",
    "cracker",
    "coon", "c00n",
    "jap",
    "towelhead",
    "zipperhead",
    "beaner",
    "gringo",
    "honky",
    "darkie",

    # Homophobic / transphobic slurs
    "faggot", "f4ggot", "fag",
    "dyke",
    "tranny", "tr4nny",
    "shemale", "she male",

    # Ableist slurs
    "retard", "ret4rd", "ret@rd",
    "retarded",
    "spastic",

    # Misogynistic slurs
    "cunt",
    "whore",
    "slut",

    # Sexual violence
    "rape", "r4pe",
    "raping",
    "molest",
    "grooming",
    "child porn", "cp",
    "loli",
    "pedo", "paedo",
    "pedophile", "paedophile",

    # Self-harm / harassment
    "kys",
    "kill yourself",
    "go kill",
    "neck yourself",
    "rope yourself",
    "drink bleach",
    "die slowly",
    "end yourself",
    "off yourself",
    "slit your",
    "cut yourself",

    # Nazi / extremist
    "heil hitler", "heil h1tler",
    "1488",
    "14 88",
    "white power",
    "white supremacy",
    "white pride",
    "kkk",
    "ku klux",
    "nazi",
    "n4zi",
]

# ── Custom words (loaded from DB at runtime) ──────────────────────────────────
_CUSTOM: list[str] = []

def load_custom_words(words: list[str]) -> None:
    """Call this at startup and after any f.censor change to refresh custom words."""
    global _CUSTOM, _ALL_PATTERNS
    _CUSTOM = list(words)
    _ALL_PATTERNS = _build_patterns(_BLOCKED + _CUSTOM)

# ── Leet normalisation ────────────────────────────────────────────────────────
_LEET_MAP = str.maketrans({
    "3": "e", "4": "a", "1": "i", "0": "o",
    "@": "a", "$": "s", "!": "i", "5": "s",
})

def _normalise(text: str) -> str:
    return text.lower().translate(_LEET_MAP)

def _make_pattern(word: str) -> re.Pattern:
    escaped = re.escape(_normalise(word))
    escaped = re.sub(r"\\ ", r"\\s+", escaped)
    return re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.IGNORECASE)

def _build_patterns(words: list[str]) -> list[re.Pattern]:
    return [_make_pattern(w) for w in words]

# Initial compile with just hardcoded words
_ALL_PATTERNS: list[re.Pattern] = _build_patterns(_BLOCKED)

# ── Public API ────────────────────────────────────────────────────────────────

def filter_message(text: str) -> tuple[str, bool]:
    """
    Apply the content filter to text.
    Returns (filtered_text, was_censored).
    """
    if not text:
        return text, False

    normalised = _normalise(text)
    censored   = False
    result     = text

    for pattern in _ALL_PATTERNS:
        if pattern.search(normalised):
            new_result = []
            last = 0
            for m in pattern.finditer(normalised):
                new_result.append(result[last:m.start()])
                new_result.append("[censored]")
                last = m.end()
                censored = True
            new_result.append(result[last:])
            result     = "".join(new_result)
            normalised = _normalise(result)

    return result, censored

def should_warn(text: str) -> bool:
    _, hit = filter_message(text)
    return hit

def get_custom_words() -> list[str]:
    """Return the current custom word list."""
    return list(_CUSTOM)
