import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.prices import NY, DailyRow, completed_rows, parse_chart, parse_spark, x1_check, x5_flags
from app.returns import build_tri

FIX = Path(__file__).parent / "fixtures" / "yahoo"


def chart(t):
    return parse_chart(json.loads((FIX / f"chart_{t}_max.json").read_text()))


def test_parse_chart_schd():
    c = chart("SCHD")
    assert c.ticker == "SCHD" and c.instrument_type == "ETF"
    assert len(c.rows) == 3763
    split_days = [r.day for r in c.rows if r.split != 1.0]
    assert split_days == [date(2024, 10, 11)]
    assert [r.split for r in c.rows if r.day == date(2024, 10, 11)] == [3.0]
    assert sum(1 for r in c.rows if r.dividend) == 60
    assert c.rows[-1].day == date(2026, 10, 8)


def test_split_does_not_create_a_fake_drop():
    rows = chart("SCHD").rows
    tri = build_tri((r.day, r.close, r.dividend) for r in rows)
    jump = tri[date(2024, 10, 11)] / tri[date(2024, 10, 10)] - 1
    assert abs(jump) < 0.05  # a mis-handled 3:1 split would show about -67%


@pytest.mark.parametrize("t", ["SPY", "SCHD", "IAU", "RSP"])
def test_x1_passes_on_real_etfs(t):
    ok, worst, _ = x1_check(chart(t).rows)
    assert ok and worst < 0.001


def test_x1_fails_when_a_dividend_is_missing():
    rows = chart("SPY").rows
    broken = [DailyRow(r.day, r.close, 0.0, r.split, r.adjclose) if r.day.year == 2020 else r for r in rows]
    ok, worst, when = x1_check(broken)
    assert not ok and worst > 0.005 and when.year >= 2020


def test_x5_flags_only_unexplained_jumps():
    assert x5_flags(chart("SPY").rows) == []
    rows = [DailyRow(date(2026, 1, 2), 10.0, 0, 1.0, None), DailyRow(date(2026, 1, 5), 16.0, 0, 1.0, None),
            DailyRow(date(2026, 1, 6), 4.0, 0, 0.25, None)]
    assert x5_flags(rows) == [date(2026, 1, 5)]


def test_completed_rows_excludes_running_session():
    rows = chart("SPY").rows
    midday = datetime(2026, 10, 8, 12, 0, tzinfo=NY)
    after = datetime(2026, 10, 8, 16, 30, tzinfo=NY)
    assert completed_rows(rows, midday)[-1].day == date(2026, 10, 7)
    assert completed_rows(rows, after)[-1].day == date(2026, 10, 8)


def test_parse_spark():
    out = parse_spark(json.loads((FIX / "spark_SPY_IAU_RSP.json").read_text()))
    assert set(out) == {"SPY", "IAU", "RSP"}
    price, when = out["SPY"]
    assert price == pytest.approx(773.93)
    assert when == datetime(2026, 10, 8, 16, 0, tzinfo=NY)


def _rows(t):
    return parse_chart(json.loads((Path(__file__).parent / "fixtures" / "spotcheck" / f"chart_{t}.json").read_text())).rows


def _corr(t="ARKK", d=date(2023, 9, 8), action="remove", amount=None):
    from app.curated import DividendCorrection
    return DividendCorrection(t, d, action, amount, "https://x.example", "e", date(2026, 10, 9))


def test_apply_dividend_corrections_remove_and_set():
    from app.prices import apply_dividend_corrections
    rows = _rows("ARKK")
    out, unmatched = apply_dividend_corrections(rows, [_corr()])
    assert unmatched == [] and next(r for r in out if r.day == date(2023, 9, 8)).dividend == 0.0
    assert next(r for r in rows if r.day == date(2023, 9, 8)).dividend == 0.364      # input untouched
    assert sum(r.dividend for r in out) == pytest.approx(sum(r.dividend for r in rows) - 0.364)
    out, _ = apply_dividend_corrections(_rows("SPY"), [_corr("SPY", date(2026, 9, 18), "set", 1.5)])
    assert next(r for r in out if r.day == date(2026, 9, 18)).dividend == 1.5


def test_apply_dividend_corrections_reports_unmatched_date():
    from app.prices import apply_dividend_corrections
    rows = _rows("SPY")
    c = _corr("SPY", date(2026, 9, 19))      # a Saturday: no bar
    out, unmatched = apply_dividend_corrections(rows, [c])
    assert out == rows and unmatched == [c]


def test_x6_flags_arkk_not_spy():
    from app.prices import x6_irregular_dividends
    assert x6_irregular_dividends(_rows("ARKK")) == [date(2021, 12, 29), date(2023, 9, 8)]
    assert x6_irregular_dividends(_rows("SPY")) == []


def test_x6_flags_dividend_over_5pct_of_prior_close():
    from app.prices import x6_irregular_dividends
    base = [DailyRow(date(2020, 1, d), 100.0, 0.0, 1.0, 100.0) for d in range(1, 10)]
    base += [DailyRow(date(2020, 2, d), 100.0, 0.5 if d % 2 else 0.0, 1.0, 100.0) for d in range(1, 11)]
    rows = base + [DailyRow(date(2020, 3, 1), 100.0, 6.0, 1.0, 100.0)]
    assert x6_irregular_dividends(rows) == [date(2020, 3, 1)]


# ---------- final review item 8: a spark batch of only invalid symbols ----------

def _spark_fetch(fail_with):
    import json as _json
    from app.http import HttpError as _HttpError

    def fetch(url, **kw):
        if "BAD" in url:
            raise _HttpError(url, fail_with, "err")
        syms = url.split("symbols=")[1].split("&")[0].split(",")
        return _json.dumps({s: {"timestamp": [1791561600], "close": [10.0]} for s in syms}).encode()
    return fetch


def test_spark_404_batch_is_skipped(monkeypatch):
    import app.prices as P
    monkeypatch.setattr(P, "fetch", _spark_fetch(404))
    tickers = [f"T{i}" for i in range(20)] + ["BAD1"]
    out = P.YahooProvider().latest(tickers)
    assert sorted(out) == sorted(tickers[:20])


def test_spark_other_errors_are_raised(monkeypatch):
    import app.prices as P
    from app.http import HttpError as _HttpError
    monkeypatch.setattr(P, "fetch", _spark_fetch(500))
    with pytest.raises(_HttpError):
        P.YahooProvider().latest([f"T{i}" for i in range(20)] + ["BAD1"])


def _payload(closes):
    ts = [1735000000 + 86400 * i for i in range(len(closes))]
    return {"chart": {"result": [{"meta": {"symbol": "ZZ", "instrumentType": "ETF"}, "timestamp": ts,
                                  "indicators": {"quote": [{"close": closes}], "adjclose": [{"adjclose": closes}]}}]}}


def test_parse_chart_drops_zero_negative_nan_and_inf_closes():
    c = parse_chart(_payload([10.0, 0, -3.5, None, float("nan"), float("inf"), 11.0]))
    assert [r.close for r in c.rows] == [10.0, 11.0]
    assert c.dropped_bad_closes == 4          # null closes are not counted


def test_parse_chart_clean_payload_counts_nothing():
    assert parse_chart(_payload([10.0, 11.0])).dropped_bad_closes == 0


def test_x5_flags_survives_zero_close():
    rows = [DailyRow(date(2026, 1, 2), 0.0, 0, 1.0, None), DailyRow(date(2026, 1, 5), 16.0, 0, 1.0, None),
            DailyRow(date(2026, 1, 6), 40.0, 0, 1.0, None)]
    assert x5_flags(rows) == [date(2026, 1, 6)]
