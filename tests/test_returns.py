from datetime import date

import pytest

from app.returns import (
    MissingPrice, TwrResult, annualize, build_tri, last_trading_day_on_or_before,
    pp_vs, rank_rows, simple_return, twr, window_start,
)

D = date


def test_build_tri_reinvests_dividend_on_ex_date():
    # close 100 -> 99 with a $1 dividend (price fell by exactly the dividend) -> no change in TRI
    tri = build_tri([(D(2026, 1, 2), 100.0, 0.0), (D(2026, 1, 5), 99.0, 1.0), (D(2026, 1, 6), 101.0, 0.0)])
    assert tri[D(2026, 1, 2)] == 1.0
    assert tri[D(2026, 1, 5)] == pytest.approx(1.0)
    assert tri[D(2026, 1, 6)] == pytest.approx(101.0 / 99.0)


def test_build_tri_rejects_bad_close():
    with pytest.raises(ValueError):
        build_tri([(D(2026, 1, 2), 100.0, 0.0), (D(2026, 1, 5), 0.0, 0.0)])


DAYS = [D(2025, 7, 2), D(2025, 7, 3), D(2025, 7, 7), D(2025, 10, 9), D(2025, 10, 10), D(2027, 2, 26), D(2027, 3, 1)]


def test_last_trading_day_on_or_before():
    assert last_trading_day_on_or_before(D(2025, 7, 4), DAYS) == D(2025, 7, 3)  # holiday
    assert last_trading_day_on_or_before(D(2025, 7, 3), DAYS) == D(2025, 7, 3)
    with pytest.raises(ValueError):
        last_trading_day_on_or_before(D(2025, 7, 1), DAYS)


def test_window_start_weekend_today():
    # today Saturday 2026-10-10 -> target 2025-10-10 (a Friday trading day)
    assert window_start(D(2026, 10, 10), 1, DAYS) == D(2025, 10, 10)


def test_window_start_holiday_target():
    assert window_start(D(2026, 7, 4), 1, DAYS) == D(2025, 7, 3)


def test_window_start_feb_29():
    # 2028-02-29 minus 1 year -> 2027-02-28 (a Sunday) -> last trading day on or before = 2027-02-26
    assert window_start(D(2028, 2, 29), 1, DAYS) == D(2027, 2, 26)


def test_simple_return_and_missing():
    tri = {D(2026, 1, 2): 2.0, D(2026, 1, 5): 2.5}
    assert simple_return(tri, D(2026, 1, 2), D(2026, 1, 5)) == pytest.approx(0.25)
    with pytest.raises(MissingPrice):
        simple_return(tri, D(2026, 1, 1), D(2026, 1, 5))


def test_annualize_three_years_of_ten_percent():
    total = 1.1 ** 3 - 1
    # 2023-10-09 -> 2026-10-09 is 1096 days; 1096/365.25 = 3.0007 years
    assert annualize(total, D(2023, 10, 9), D(2026, 10, 9)) == pytest.approx(0.1, abs=2e-4)


def test_pp_vs():
    assert pp_vs(0.842, 0.532) == pytest.approx(31.0)


d0, d1, d2, d3 = D(2026, 1, 2), D(2026, 1, 5), D(2026, 1, 6), D(2026, 1, 7)
TD = [d0, d1, d2, d3]
BOOK = {
    "A": {d0: 1.0, d1: 1.1, d2: 1.21, d3: 1.21},
    "B": {d0: 1.0, d1: 1.0, d2: 0.5, d3: 0.5},
}


def test_twr_with_mid_window_buy():
    # hold 100 A from d0; buy 110 units of B (worth $110) at close of d1
    res = twr([(d0, "A", 100.0), (d1, "B", 110.0)], BOOK, TD, d0, d3)
    # d1: A +10%. d2: V_prev = 100*1.1 + 110*1.0 = 220; V_now = 121 + 55 = 176 -> -20%. d3: 0%.
    assert res.total == pytest.approx(1.1 * 0.8 - 1)
    assert (res.invested_days, res.total_days) == (3, 3)
    assert res.invested_fraction == 1.0


def test_twr_empty_period_counts_as_not_invested():
    res = twr([(d1, "A", 100.0)], BOOK, TD, d0, d3)
    assert res.total == pytest.approx(1.21 / 1.1 - 1)
    assert (res.invested_days, res.total_days) == (2, 3)


def test_twr_full_sale():
    res = twr([(d0, "A", 100.0), (d2, "A", 0.0)], BOOK, TD, d0, d3)
    assert res.total == pytest.approx(0.21)
    assert res.invested_days == 2


def test_twr_changes_before_start_set_starting_holdings():
    res = twr([(d0, "A", 5.0)], BOOK, TD, d1, d3)
    assert res.total == pytest.approx(1.21 / 1.1 - 1)
    assert res.total_days == 2


def test_twr_never_invested_has_no_total():
    res = twr([], BOOK, TD, d0, d3)
    assert res == TwrResult(total=None, invested_days=0, total_days=3)


def test_twr_rejects_change_on_non_trading_day_inside_window():
    with pytest.raises(ValueError):
        twr([(d0, "A", 1.0), (D(2026, 1, 3), "A", 2.0)], BOOK, TD, d0, d3)


def test_twr_missing_price_raises():
    book = {"A": {d0: 1.0, d1: 1.1}}
    with pytest.raises(MissingPrice) as e:
        twr([(d0, "A", 1.0)], book, TD, d0, d3)
    assert e.value.ticker == "A" and e.value.day == d2


def test_twr_series_for_chart():
    res = twr([(d0, "A", 1.0)], BOOK, TD, d0, d3, with_series=True)
    assert [d for d, _ in res.series] == TD
    assert [round(g, 6) for _, g in res.series] == [1.0, 1.1, 1.21, 1.21]


def test_rank_rows_ties_alphabetical():
    rows = [{"name": "bob", "total": 0.1}, {"name": "Amy", "total": 0.1}, {"name": "Cy", "total": 0.5}]
    ranked = rank_rows(rows)
    assert [(r["rank"], r["name"]) for r in ranked] == [(1, "Cy"), (2, "Amy"), (3, "bob")]


# ---------- final review item 5: the faster twr is bit-for-bit the original ----------

def _twr_reference(changes, tri_book, trading_days, start, end, with_series=False):
    """The original (pre-optimisation) implementation, kept verbatim as the oracle."""
    import bisect
    days = trading_days[bisect.bisect_right(trading_days, start):bisect.bisect_right(trading_days, end)]
    trading_set = set(trading_days)
    pending = sorted(changes, key=lambda c: c[0])
    units = {}
    i = 0
    while i < len(pending) and pending[i][0] <= start:
        units[pending[i][1]] = pending[i][2]
        i += 1
    growth, invested, prev = 1.0, 0, start
    series = [(start, 1.0)]

    def px(k, d):
        try:
            return tri_book[k][d]
        except KeyError:
            raise MissingPrice(k, d) from None
    for t in days:
        held = {k: u for k, u in units.items() if u > 0}
        if held:
            v_prev = sum(u * px(k, prev) for k, u in held.items())
            v_now = sum(u * px(k, t) for k, u in held.items())
            growth *= v_now / v_prev
            invested += 1
        while i < len(pending) and pending[i][0] <= t:
            day, ticker, u = pending[i]
            if day not in trading_set:
                raise ValueError(f"unit change on non-trading day {day} inside the window")
            units[ticker] = u
            i += 1
        prev = t
        series.append((t, growth))
    return TwrResult(total=(growth - 1) if invested else None, invested_days=invested, total_days=len(days),
                     series=tuple(series) if with_series else ())


@pytest.mark.parametrize("seed", range(100))
def test_twr_matches_the_original_exactly(seed):
    import math
    import random
    from array import array
    from datetime import timedelta
    from app.leaderboards import DenseSeries
    rnd = random.Random(seed)
    days, d = [], D(2025, 1, 2)
    while len(days) < 120:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    idx = {x: i for i, x in enumerate(days)}
    tickers = [f"T{i}" for i in range(6)]
    book = {}
    for k in tickers:
        v, vals = 1.0, array("d")
        for _ in days:
            v *= 1 + rnd.gauss(0.0005, 0.02)
            vals.append(v)
        if rnd.random() < 0.3:                        # a gap: no price on some days
            for j in rnd.sample(range(len(days)), 3):
                vals[j] = math.nan
        if rnd.random() < 0.2:                        # a short array (a ticker that started later)
            vals = vals[:rnd.randrange(60, 120)]
        book[k] = DenseSeries(idx, vals) if seed % 2 == 0 else {x: vals[i] for i, x in enumerate(days)
                                                               if i < len(vals) and not math.isnan(vals[i])}
    changes = [(rnd.choice(days), rnd.choice(tickers), rnd.choice([0.0, rnd.uniform(1, 100)])) for _ in range(25)]
    start, end = days[rnd.randrange(0, 30)], days[rnd.randrange(80, 120)]
    try:
        want = _twr_reference(changes, book, days, start, end, with_series=True)
    except MissingPrice as e:
        with pytest.raises(MissingPrice) as got:
            twr(changes, book, days, start, end, with_series=True)
        assert (got.value.ticker, got.value.day) == (e.ticker, e.day)
        return
    got = twr(changes, book, days, start, end, with_series=True)
    assert got == want                               # exact float equality, series included
