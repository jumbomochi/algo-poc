from __future__ import annotations

import re

# Tickers that are also common English words/acronyms: a bare uppercase
# mention is almost always the word, so they only count as cashtags.
# Single-letter tickers (V, ...) are handled by the len >= 2 rule below.
AMBIGUOUS_TICKERS = {
    "AI", "ALL", "AN", "AT", "BE", "CAN", "DO", "GO", "IT", "LOW",
    "NOW", "ON", "ONE", "OR", "SO", "TAN", "UPS", "ICE", "HACK",
    "HERO", "LIT", "GE", "KO", "MA", "MO", "PM", "SH",
}

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_BARE_RE = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text: str, universe: set[str]) -> set[str]:
    """Extract watchlist tickers from free text.

    Cashtags ($TSLA, case-insensitive) match any universe ticker. Bare
    uppercase words match only unambiguous universe tickers of length >= 2.
    """
    found: set[str] = set()
    for match in _CASHTAG_RE.finditer(text):
        symbol = match.group(1).upper()
        if symbol in universe:
            found.add(symbol)
    for match in _BARE_RE.finditer(text):
        symbol = match.group(1)
        if symbol in universe and symbol not in AMBIGUOUS_TICKERS:
            found.add(symbol)
    return found
