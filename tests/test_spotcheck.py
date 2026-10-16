import json
from datetime import date
from pathlib import Path

import pytest

from app.curated import DIVIDEND_CORRECTIONS_PATH, load_dividend_corrections
from app.prices import apply_dividend_corrections, parse_chart
from app.returns import annualize, build_tri, simple_return, window_start

FIX = Path(__file__).parent / "fixtures" / "spotcheck"
PUB = json.loads((FIX / "published.json").read_text())


@pytest.mark.parametrize("ticker", sorted(PUB["funds"]))
def test_matches_issuer_market_price_returns(ticker):
    pub = PUB["funds"][ticker]
    as_of = date.fromisoformat(pub.get("as_of", PUB["as_of"]))
    rows = [r for r in parse_chart(json.loads((FIX / f"chart_{ticker}.json").read_text())).rows if r.day <= as_of]
    rows, unmatched = apply_dividend_corrections(
        rows, [c for c in load_dividend_corrections(DIVIDEND_CORRECTIONS_PATH, date.today()) if c.ticker == ticker])
    assert not unmatched
    tri = build_tri((r.day, r.close, r.dividend) for r in rows)
    days = [r.day for r in rows]
    end = days[-1]
    assert end == as_of, "month-end must be a trading day in the data"
    for years, key in ((1, "1y"), (3, "3y_annualized"), (5, "5y_annualized")):
        start = window_start(as_of, years, days)
        total = simple_return(tri, start, end)
        ours = total if years == 1 else annualize(total, start, end)
        assert abs(ours - pub[key]) * 100 <= 0.1, (ticker, key, ours, pub[key])   # within 0.1 percentage points
