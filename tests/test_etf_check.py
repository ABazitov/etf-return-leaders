import json
from pathlib import Path

import pytest

from app.etf_check import (
    etf_name_index, is_etf, looks_like_etf, match_by_name, normalize_name, parse_nasdaq_directory, parse_openfigi,
    ticker_candidates,
)

FIX = Path(__file__).parent / "fixtures"


def test_parse_openfigi_cusips():
    hits = parse_openfigi(json.loads((FIX / "openfigi" / "cusip_batch.json").read_text()))
    assert [(h.ticker, h.security_type) if h else None for h in hits] == [
        ("SPY", "ETP"), ("IAU", "ETP"), ("CSCO", "Common Stock"), None]


def test_parse_openfigi_tickers():
    hits = parse_openfigi(json.loads((FIX / "openfigi" / "ticker_batch.json").read_text()))
    assert [(h.ticker, h.security_type, h.exch) for h in hits] == [("RSP", "ETP", "US"), ("MSFT", "Common Stock", "US")]


def test_nasdaq_directory():
    d = parse_nasdaq_directory((FIX / "reference" / "nasdaqtraded_sample.txt").read_text())
    assert d["SPY"] == ("State Street SPDR S&P 500 ETF Trust", True)
    assert d["MSFT"][1] is False
    assert "File Creation Time: 1008202617:02" not in d and len(d) == 10


@pytest.mark.parametrize("figi,yahoo,name,expected", [
    ("ETP", "ETF", "SPDR S&P 500 ETF Trust", True),
    ("ETP", "ETF", "iPath Series B S&P 500 VIX Short-Term Futures ETN", False),
    ("ETP", "ETF", "Some Exchange Traded Notes", False),
    ("ETP", "EQUITY", "Odd one", False),
    ("Common Stock", "ETF", "Odd two", False),
    (None, "ETF", "Unknown", False),
])
def test_is_etf(figi, yahoo, name, expected):
    assert is_etf(figi, yahoo, name) is expected


@pytest.mark.parametrize("text,given,expected", [
    ("SPDR S&P 500 ETF Trust (SPY)", None, ["SPY"]),
    ("Alphabet Inc. - Class A Common Stock (GOOGL)", None, ["GOOGL"]),
    ("Berkshire Hathaway Inc. New (BRK.B)", None, ["BRK.B"]),
    ("RSP ETF", None, ["RSP"]),
    ("New Water Capital Partners II, LP (GLAS Funds LP)", None, []),
    ("Invesco QQQ Trust", "QQQ", ["QQQ"]),
    ("Invesco QQQ Trust (QQQ)", "--", ["QQQ"]),
])
def test_ticker_candidates(text, given, expected):
    assert ticker_candidates(text, given) == expected


def test_match_by_name_exact_and_unique():
    d = etf_name_index(parse_nasdaq_directory((FIX / "reference" / "nasdaqtraded_sample.txt").read_text()))
    assert match_by_name("SPDR Gold Shares", d) == "GLD"
    assert match_by_name("spdr gold shares.", d) == "GLD"
    assert match_by_name("iShares Gold Trust", d) is None          # directory says "...Trust Shares"
    assert match_by_name("Microsoft Corporation - Common Stock", d) is None  # not an ETF
    two = etf_name_index({"AAA": ("Same Fund ETF", True), "BBB": ("Same Fund ETF", True), "CCC": ("Other", False)})
    assert match_by_name("same fund etf", two) is None                  # two ETFs share the name: not unique
    assert match_by_name("other", two) is None                          # not an ETF


def test_normalize_and_looks_like_etf():
    assert normalize_name("  Invesco QQQ Trust, Series 1 ") == "invesco qqq trust series 1"
    assert looks_like_etf("Vanguard Total Stock Market Index") is True
    assert looks_like_etf("Invesco QQQ Trust, Series 1") is True
    assert looks_like_etf("Apple Inc. Common Stock") is False


def test_looks_like_etf_requires_etf_word_or_fund_family():
    for yes in ("SPDR S&P 500 ETF Trust", "iShares Core S&P 500", "Vanguard Total Stock Market Index",
                "Invesco QQQ Trust, Series 1", "Schwab US Dividend Equity", "Schwab ETF Fund", "ProShares UltraPro",
                "Direxion Daily Bull", "ARK Innovation", "Global X Lithium", "WisdomTree Japan", "VanEck Gold Miners",
                "First Trust Cloud Computing", "Some Index Fund", "A Exchange Traded Product"):
        assert looks_like_etf(yes) is True, yes
    for no in ("Accenture plc Class A Ordinary Shares (ACN)", "Medtronic plc. Ordinary Shares (MDT)",
               "Charles Schwab Corporation (SCHW)", "Invesco Ltd (IVZ)", "Apple Inc. Common Stock",
               "Realty Income Trust", "Fidelity Magellan Fund", "S&P Index Holdings", "Markel Group"):
        assert looks_like_etf(no) is False, no
