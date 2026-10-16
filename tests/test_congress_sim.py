from datetime import date

import pytest

from app.congress_sim import TX_KIND, Amount, SimTrade, parse_amount, simulate

D = date


@pytest.mark.parametrize("text,low,high,open_ended,exact", [
    ("$1,001 - $15,000", 1001, 15000, False, False),
    ("$50,001 -  $100,000", 50001, 100000, False, False),
    ("$25,000,001 - $50,000,000", 25000001, 50000000, False, False),
    ("Over $50,000,000", 50000001, 50000001, True, False),
    ("Spouse/DC Over $1,000,000", 1000001, 1000001, True, False),
    ("$318.74", 318.74, 318.74, False, True),
])
def test_parse_amount(text, low, high, open_ended, exact):
    a = parse_amount(text)
    assert (a.low, a.high, a.open_ended, a.exact) == (low, high, open_ended, exact)


def test_midpoint_example_from_spec():
    assert parse_amount("$1,001 - $15,000").mid == 8000.5


def test_parse_amount_rejects_unknown():
    with pytest.raises(ValueError):
        parse_amount("about ten grand")


def test_tx_kinds_cover_both_chambers():
    assert TX_KIND["P"] == "buy" and TX_KIND["Purchase"] == "buy"
    assert TX_KIND["S"] == "sell_full" and TX_KIND["Sale (Full)"] == "sell_full"
    assert TX_KIND["S (partial)"] == "sell_partial" and TX_KIND["Sale (Partial)"] == "sell_partial"
    assert TX_KIND["E"] is None and TX_KIND["Exchange"] is None


d1, d2, d3 = D(2026, 1, 5), D(2026, 1, 6), D(2026, 1, 7)
BOOK = {"SPY": {d1: 2.0, d2: 2.5, d3: 4.0}}
A10k = Amount(10000, 10000, False, True)
A5k = Amount(5000, 5000, False, True)


def test_buy_then_partial_then_full_sale():
    trades = [
        SimTrade(d1, "SPY", "buy", A10k, "r1"),           # 10000 / 2.0 = 5000 units
        SimTrade(d2, "SPY", "sell_partial", A5k, "r2"),   # 5000 / 2.5 = 2000 units -> 3000 left
        SimTrade(d3, "SPY", "sell_full", A5k, "r3"),      # -> 0
    ]
    changes, notes = simulate(trades, BOOK)
    assert changes == [(d1, "SPY", 5000.0), (d2, "SPY", 3000.0), (d3, "SPY", 0.0)]
    assert notes == []


def test_partial_sale_capped_at_units_held():
    trades = [SimTrade(d1, "SPY", "buy", A5k, "r1"),                              # 2500 units
              SimTrade(d3, "SPY", "sell_partial", Amount(100000, 100000, False, True), "r2")]
    changes, _ = simulate(trades, BOOK)
    assert changes[-1] == (d3, "SPY", 0.0)


def test_sale_without_position_is_ignored_and_noted():
    changes, notes = simulate([SimTrade(d2, "SPY", "sell_full", A5k, "r9")], BOOK)
    assert changes == []
    assert notes == [("r9", "sale of a position bought before the disclosure record")]


def test_low_and_high_variants_use_range_ends():
    a = parse_amount("$1,001 - $15,000")
    lo, _ = simulate([SimTrade(d1, "SPY", "buy", a, "r1")], BOOK, which="low")
    hi, _ = simulate([SimTrade(d1, "SPY", "buy", a, "r1")], BOOK, which="high")
    assert lo == [(d1, "SPY", 1001 / 2.0)]
    assert hi == [(d1, "SPY", 15000 / 2.0)]
