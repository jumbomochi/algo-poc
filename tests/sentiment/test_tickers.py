from __future__ import annotations

from sentiment.tickers import extract_tickers

UNIVERSE = {"AAPL", "TSLA", "NVDA", "IT", "NOW", "ALL", "V", "MA", "KO", "LOW", "ICE", "TAN", "HACK", "BRK B"}


def test_cashtag_hits():
    assert extract_tickers("$TSLA and $aapl look strong", UNIVERSE) == {"TSLA", "AAPL"}


def test_cashtag_outside_universe_ignored():
    assert extract_tickers("$GME squeeze!", UNIVERSE) == set()


def test_bare_symbol_in_universe():
    assert extract_tickers("NVDA earnings tomorrow", UNIVERSE) == {"NVDA"}


def test_bare_lowercase_not_matched():
    assert extract_tickers("nvda earnings tomorrow", UNIVERSE) == set()


def test_ambiguous_words_need_cashtag():
    # IT/NOW/ALL/LOW/ICE/TAN/HACK are real tickers but also common words:
    # bare mentions are ignored, cashtags still count.
    assert extract_tickers("IT is ALL over NOW, buy LOW", UNIVERSE) == set()
    assert extract_tickers("$NOW crushed earnings", UNIVERSE) == {"NOW"}


def test_single_letter_needs_cashtag():
    assert extract_tickers("V for victory", UNIVERSE) == set()
    assert extract_tickers("long $V and $MA", UNIVERSE) == {"V", "MA"}


def test_no_false_positive_on_substrings():
    assert extract_tickers("SNOWFLAKE is not NOW", UNIVERSE) == set()
