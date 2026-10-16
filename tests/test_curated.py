import json
from datetime import date

import pytest

from app.curated import (
    DIVIDEND_CORRECTIONS_PATH, FAMOUS_PATH, MANAGERS_PATH, CuratedError, is_stale, load_dividend_corrections,
    load_famous, load_managers, tenure_start,
)

TODAY = date(2026, 10, 9)
GOOD_M = {"person": "Jane Example", "ticker": "AAAA", "fund_name": "Example ETF", "since": "2014-10",
          "source_url": "https://example.com/p.pdf", "evidence": "has managed the Fund since October 2014.",
          "verified": "2026-10-09"}
GOOD_F = {"cik": "1067983", "filer_name": "BERKSHIRE HATHAWAY INC", "person": "Warren Buffett",
          "role": "Chairman and CEO", "source_url": "https://www.sec.gov/x", "verified": "2026-10-09"}


@pytest.mark.parametrize("text,expected", [
    ("2014", date(2014, 12, 31)), ("2014-10", date(2014, 10, 31)), ("2024-02", date(2024, 2, 29)),
    ("2014-10-03", date(2014, 10, 3)),
])
def test_tenure_start_is_conservative(text, expected):
    assert tenure_start(text) == expected


def write(tmp_path, rows):
    p = tmp_path / "x.json"
    p.write_text(json.dumps(rows))
    return p


def test_load_valid(tmp_path):
    [m] = load_managers(write(tmp_path, [GOOD_M]), TODAY)
    assert (m.ticker, m.tenure_start, m.verified) == ("AAAA", date(2014, 10, 31), TODAY)
    [f] = load_famous(write(tmp_path, [GOOD_F]), TODAY)
    assert f.cik == "1067983"


@pytest.mark.parametrize("field,value", [
    ("since", "October 2014"), ("source_url", "http://insecure.example.com"), ("evidence", ""),
    ("verified", "2027-01-01"), ("ticker", "aaaa"), ("person", ""),
])
def test_rejects_bad_manager(tmp_path, field, value):
    with pytest.raises(CuratedError):
        load_managers(write(tmp_path, [{**GOOD_M, field: value}]), TODAY)


def test_rejects_duplicate_person_fund(tmp_path):
    with pytest.raises(CuratedError):
        load_managers(write(tmp_path, [GOOD_M, GOOD_M]), TODAY)


@pytest.mark.parametrize("field,value", [("cik", "01067983"), ("cik", "abc"), ("role", ""), ("person", "")])
def test_rejects_bad_famous(tmp_path, field, value):
    with pytest.raises(CuratedError):
        load_famous(write(tmp_path, [{**GOOD_F, field: value}]), TODAY)


def test_is_stale():
    assert is_stale(date(2025, 10, 8), TODAY) and not is_stale(date(2025, 10, 10), TODAY)


def test_shipped_lists_are_valid():
    load_managers(MANAGERS_PATH, TODAY)
    load_famous(FAMOUS_PATH, TODAY)


GOOD_D = {"ticker": "ARKK", "ex_date": "2023-09-08", "action": "remove", "source_url": "https://www.sec.gov/x",
          "evidence": "Distributions: none", "verified": "2026-10-09"}


def test_load_dividend_corrections(tmp_path):
    [a, b] = load_dividend_corrections(write(tmp_path, [GOOD_D, {**GOOD_D, "ex_date": "2023-09-09", "action": "set", "amount": 0.1}]), TODAY)
    assert (a.ticker, a.ex_date, a.action, a.amount) == ("ARKK", date(2023, 9, 8), "remove", None)
    assert (b.action, b.amount) == ("set", 0.1)


@pytest.mark.parametrize("patch", [
    {"action": "set"}, {"action": "set", "amount": -1}, {"source_url": "http://x.com"}, {"ex_date": "09/08/2023"},
    {"action": "delete"}, {"evidence": ""}, {"ticker": "arkk"}, {"verified": "2027-01-01"},
])
def test_rejects_bad_dividend_correction(tmp_path, patch):
    with pytest.raises(CuratedError):
        load_dividend_corrections(write(tmp_path, [{**GOOD_D, **patch}]), TODAY)


def test_rejects_duplicate_dividend_correction(tmp_path):
    with pytest.raises(CuratedError):
        load_dividend_corrections(write(tmp_path, [GOOD_D, GOOD_D]), TODAY)


def test_shipped_dividend_corrections_are_valid():
    out = load_dividend_corrections(DIVIDEND_CORRECTIONS_PATH, TODAY)
    assert [(c.ticker, c.ex_date, c.action) for c in out][0] == ("ARKK", date(2023, 9, 8), "remove")
