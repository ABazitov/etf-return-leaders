"""Final review items 1-2: the live day in load_book (R3/R4), including a split on the live day."""
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from app import db
from app.config import Config
from app.curated import ManagerFund
from app.leaderboards import make_window, managers_board
from app.pipeline import Ctx, load_book, sync_dividends_today
from app.prices import NY, Chart, DailyRow
from app.returns import simple_return

NOW = datetime(2026, 10, 9, 11, 0, tzinfo=NY)          # Friday, market open
TODAY, YESTERDAY = NOW.date(), date(2026, 10, 8)


def _days():
    d, out = date(2024, 1, 2), []
    while d <= YESTERDAY:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.fixture
def ctx(tmp_path):
    cfg = Config("a@b.co", "", False, False, 8765, tmp_path)
    return Ctx(cfg, db.connect(tmp_path / "t.db"), tmp_path, now=lambda: NOW)


def _daily(ctx, ticker, last_close=None):
    days = _days()
    rows = [(ticker, d.isoformat(), 100 + i * 0.01, 0, 1, None) for i, d in enumerate(days)]
    if last_close is not None:
        rows[-1] = (ticker, days[-1].isoformat(), last_close, 0, 1, None)
    with db.transaction(ctx.conn):
        ctx.conn.executemany("INSERT INTO prices_daily VALUES(?,?,?,?,?,?)", rows)


def _live(ctx, ticker, price, when=NOW):
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT OR REPLACE INTO prices_live VALUES(?,?,?)", (ticker, price, when.isoformat()))


def _fund(person, ticker):
    return ManagerFund(person, ticker, ticker, "2015", date(2015, 12, 31), "https://x", "e", date(2026, 1, 1))


# ---------- item 1: carry the last close forward ----------

def test_fund_without_live_quote_today_is_priced_at_last_close(ctx):
    for t in ("SPY", "AAA", "BBB"):
        _daily(ctx, t)
    _live(ctx, "SPY", 108.0)
    _live(ctx, "AAA", 108.0)
    _live(ctx, "BBB", 107.0, NOW - timedelta(days=1))     # illiquid: no trade yet today
    book = load_book(ctx, {"SPY", "AAA", "BBB"})
    assert book.trading_days[-1] == TODAY
    assert book.has("BBB", TODAY)
    assert book.tri["BBB"][TODAY] == book.tri["BBB"][YESTERDAY]
    assert book.close["BBB"][TODAY] == book.close["BBB"][YESTERDAY]
    w = make_window("1y", book, TODAY)
    assert w.end == TODAY
    spy = simple_return(book.tri["SPY"], w.start, w.end)
    ranked, unranked = managers_board([_fund("Alice", "AAA"), _fund("Bob", "BBB")], book, w, spy)
    assert unranked == [] and {r["name"] for r in ranked} == {"Alice", "Bob"}
    bob = next(r for r in ranked if r["name"] == "Bob")
    assert bob["total"] == simple_return(book.tri["BBB"], w.start, YESTERDAY)
    alice = next(r for r in ranked if r["name"] == "Alice")
    assert alice["total"] == pytest.approx(book.tri["AAA"][YESTERDAY] * 108.0 / book.close["AAA"][YESTERDAY]
                                           / book.tri["AAA"][w.start] - 1, rel=1e-12)


def test_ticker_with_no_live_row_at_all_is_carried_forward(ctx):
    _daily(ctx, "SPY")
    _daily(ctx, "CCC")
    _live(ctx, "SPY", 108.0)
    book = load_book(ctx, {"SPY", "CCC"})
    assert book.tri["CCC"][TODAY] == book.tri["CCC"][YESTERDAY]


# ---------- item 2: a split on the live day ----------

def _split_book(ctx, last_close, price, ratio):
    _daily(ctx, "SPY")
    _daily(ctx, "AAA", last_close=last_close)
    _live(ctx, "SPY", 108.0)
    _live(ctx, "AAA", price)
    if ratio is not None:
        with db.transaction(ctx.conn):
            db.set_meta(ctx.conn, f"split-today:{TODAY.isoformat()}", '{"AAA": %r}' % ratio)
    return load_book(ctx, {"SPY", "AAA"})


def test_three_for_one_split_today_gives_no_artificial_drop(ctx):
    book = _split_book(ctx, last_close=90.0, price=30.3, ratio=3.0)      # 30.3 new shares = 90.9 per old share
    change = book.tri["AAA"][TODAY] / book.tri["AAA"][YESTERDAY] - 1
    assert change == pytest.approx(0.01, rel=1e-9)
    assert book.close["AAA"][TODAY] == pytest.approx(90.9, rel=1e-12)    # in the same (pre-split) units as history


def test_one_for_ten_reverse_split_today_gives_no_artificial_jump(ctx):
    book = _split_book(ctx, last_close=2.0, price=20.4, ratio=0.1)       # 20.4 per new share = 2.04 per old share
    change = book.tri["AAA"][TODAY] / book.tri["AAA"][YESTERDAY] - 1
    assert change == pytest.approx(0.02, rel=1e-9)


def test_split_jump_with_unknown_ratio_is_not_used_live(ctx):
    book = _split_book(ctx, last_close=90.0, price=30.0, ratio=None)     # looks like 3:1 but no ratio recorded
    assert book.tri["AAA"][TODAY] == book.tri["AAA"][YESTERDAY]
    assert book.close["AAA"][TODAY] == 90.0


def test_split_with_dividend_today_uses_old_share_units(ctx):
    _daily(ctx, "SPY")
    _daily(ctx, "AAA", last_close=90.0)
    _live(ctx, "SPY", 108.0)
    _live(ctx, "AAA", 30.0)
    with db.transaction(ctx.conn):
        db.set_meta(ctx.conn, f"split-today:{TODAY.isoformat()}", '{"AAA": 3.0}')
        db.set_meta(ctx.conn, f"div-today:{TODAY.isoformat()}", '{"AAA": 0.3}')    # per new share, as Yahoo reports it
    book = load_book(ctx, {"SPY", "AAA"})
    assert book.tri["AAA"][TODAY] / book.tri["AAA"][YESTERDAY] == pytest.approx((30.0 + 0.3) * 3.0 / 90.0, rel=1e-12)


class SplitYahoo:
    def __init__(self, ratio):
        self.ratio = ratio

    def history(self, t, recent=False):
        rows = [DailyRow(YESTERDAY, 90.0, 0.0, 1.0, 90.0), DailyRow(TODAY, 30.0, 0.0, self.ratio, 30.0)]
        return Chart(t, "ETF", rows if t == "AAA" else [replace(r, split=1.0) for r in rows], None)


def test_dividends_job_records_todays_split_ratio(ctx):
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO congress_trades(report_id, row, ticker, is_etf, is_option) VALUES('X',1,'AAA',1,0)")
    ctx.yahoo = SplitYahoo(3.0)
    out = sync_dividends_today(ctx)
    assert out["split_today"] == 1
    assert db.get_meta(ctx.conn, f"split-today:{TODAY.isoformat()}") == '{"AAA": 3.0}'


def test_history_refetched_after_the_split_is_not_adjusted_twice(ctx):
    # e.g. a mid-day full re-fetch: Yahoo's history is already in post-split units (c(L) = 30, not 90)
    book = _split_book(ctx, last_close=30.0, price=30.3, ratio=3.0)
    assert book.tri["AAA"][TODAY] / book.tri["AAA"][YESTERDAY] - 1 == pytest.approx(0.01, rel=1e-9)
    assert book.close["AAA"][TODAY] == 30.3
    assert book.split_factor_after("AAA", YESTERDAY) == 3.0     # yesterday's quoted price was 90, not the stored 30


def test_history_not_yet_refetched_on_split_day_needs_no_factor(ctx):
    book = _split_book(ctx, last_close=90.0, price=30.3, ratio=3.0)     # history still in pre-split units
    assert book.split_factor_after("AAA", YESTERDAY) == 1.0


def test_stored_splits_are_in_the_book(ctx):
    for t in ("SPY", "AAA", "BBB"):
        _daily(ctx, t)
    with db.transaction(ctx.conn):
        ctx.conn.execute("UPDATE prices_daily SET split=2.0 WHERE ticker IN ('AAA', 'BBB') AND day='2025-06-02'")
    book = load_book(ctx, {"SPY", "AAA"})
    assert book.split_factor_after("AAA", date(2025, 5, 30)) == 2.0
    assert book.split_factor_after("AAA", date(2025, 6, 2)) == 1.0
    assert book.split_factor_after("SPY", date(2025, 5, 30)) == 1.0
    assert "BBB" not in book.splits                                  # not asked for


def test_store_chart_on_split_day_marks_history_as_post_split(ctx):
    from app.pipeline import _store_chart
    rows = [DailyRow(d, 30.0, 0.0, 1.0, 30.0) for d in _days()]
    rows.append(DailyRow(TODAY, 30.3, 0.0, 1.25, 30.3))      # in-progress session, with a 5-for-4 split
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO securities(ticker, name, yahoo_type, figi_type, is_etf, price_ok, evidence, "
                         "checked_at) VALUES('AAA','AAA','ETF','ETP',1,1,'{}','x')")
        db.set_meta(ctx.conn, f"split-today:{TODAY.isoformat()}", '{"AAA": 1.25}')
    _store_chart(ctx, Chart("AAA", "ETF", rows, None))
    _daily(ctx, "SPY")
    _live(ctx, "SPY", 108.0)
    _live(ctx, "AAA", 30.3)
    book = load_book(ctx, {"SPY", "AAA"})
    assert book.tri["AAA"][TODAY] / book.tri["AAA"][YESTERDAY] == pytest.approx(1.01, rel=1e-12)   # not x 1.25
