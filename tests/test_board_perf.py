"""Final review item 5: build_boards speed, and the $9M reported-value pre-filter (which follows from X2 and F8)."""
import json
import random
import time
from datetime import date, datetime, timedelta

import pytest

from app import db, pipeline
from app.config import Config
from app.leaderboards import quarter_ends_between
from app.pipeline import Ctx
from app.prices import NY

NOW = datetime(2026, 10, 9, 17, 0, tzinfo=NY)


def _days(start=date(2012, 1, 3)):
    d, out = start, []
    while d <= NOW.date():
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _ctx(tmp_path):
    cfg = Config("a@b.co", "", False, False, 8765, tmp_path)
    return Ctx(cfg, db.connect(tmp_path / "t.db"), tmp_path, now=lambda: NOW)


def _paths(tmp_path, famous=()):
    m, f = tmp_path / "m.json", tmp_path / "f.json"
    m.write_text("[]")
    f.write_text(json.dumps([{"cik": c, "filer_name": "F", "person": "P", "role": "CEO", "source_url": "https://x",
                              "evidence": "x", "verified": "2026-10-01"} for c in famous]))
    return m, f


def _synthetic(conn, n_tickers, n_filers, n_pos=40, seed=1):
    """Same shape as the review's perf.py: random-walk ETFs with quarterly dividends, filers with 40 ETFs each."""
    rnd = random.Random(seed)
    days = _days()
    tickers = ["SPY"] + [f"T{i}" for i in range(n_tickers)]
    closes = {}
    with db.transaction(conn):
        for t in tickers:
            p, rows = 100.0, []
            for i, d in enumerate(days):
                p *= 1 + rnd.gauss(0.0003, 0.01)
                rows.append((t, d.isoformat(), p, 0.2 if i % 63 == 0 else 0, 1, p))
                closes[(t, d)] = p
            conn.executemany("INSERT INTO prices_daily VALUES(?,?,?,?,?,?)", rows)
            conn.execute("INSERT INTO securities VALUES(?,?,?,?,1,1,'{}','2026-10-01')", (t, t + " ETF", "ETF", "ETP"))
            conn.execute("INSERT INTO cusip_map VALUES(?,?,?,?,?,?)", ("C" + t, t, "ETP", "US", "{}", "x"))
        periods = quarter_ends_between(date(2021, 9, 30), date(2026, 6, 30))
        for f in range(n_filers):
            cik = str(100000 + f)
            held = rnd.sample(tickers, n_pos)
            for p in periods:
                acc = f"{cik}-{p.isoformat()}"
                last = max(x for x in days if x <= p)
                conn.execute("INSERT INTO f13_filings VALUES(?,?,?,?,?,?,?)",
                             (acc, cik, f"Filer {f}", p.isoformat(), (p + timedelta(days=40)).isoformat(), "13F-HR", None))
                conn.execute("INSERT INTO f13_filing_totals VALUES(?,?,?)", (acc, n_pos, 1e9))
                conn.executemany("INSERT INTO f13_holdings VALUES(?,?,?,?,?,?)",
                                 [(acc, "C" + t, 10000, 10000 * closes[(t, last)], t, "ETF") for t in held])


def test_build_boards_is_fast_enough_at_two_percent_scale(tmp_path):
    ctx = _ctx(tmp_path)
    _synthetic(ctx.conn, 60, 180)
    m, f = _paths(tmp_path)
    t0 = time.perf_counter()
    out = pipeline.build_boards(ctx, m, f)
    elapsed = time.perf_counter() - t0
    assert out["investors_ranked"] == 180
    assert elapsed < 2.5, f"build_boards took {elapsed:.1f} s"     # 3.4 s before the final-review speed-ups


# ---------- the $9M pre-filter ----------

def _filer(conn, cik, shares_value, reported_factor, periods, days, closes):
    """One filer holding one ETF; reported value = factor x shares x close (X2-consistent within 10%)."""
    for p in periods:
        acc = f"{cik}-{p.isoformat()}"
        last = max(x for x in days if x <= p)
        shares = shares_value / closes[last]
        conn.execute("INSERT INTO f13_filings VALUES(?,?,?,?,?,?,?)",
                     (acc, cik, f"Filer {cik}", p.isoformat(), (p + timedelta(days=40)).isoformat(), "13F-HR", None))
        conn.execute("INSERT INTO f13_filing_totals VALUES(?,?,?)", (acc, 1, 1e9))
        conn.execute("INSERT INTO f13_holdings VALUES(?,?,?,?,?,?)",
                     (acc, "CAAA", shares, shares * closes[last] * reported_factor, "AAA", "ETF"))


@pytest.fixture
def prefilter_db(tmp_path):
    ctx = _ctx(tmp_path)
    days = _days(date(2019, 1, 2))
    closes = {d: 50 * 1.0002 ** i for i, d in enumerate(days)}
    with db.transaction(ctx.conn):
        for t in ("SPY", "AAA"):
            ctx.conn.executemany("INSERT INTO prices_daily VALUES(?,?,?,?,?,?)",
                                 [(t, d.isoformat(), closes[d], 0, 1, closes[d]) for d in days])
            ctx.conn.execute("INSERT INTO securities VALUES(?,?,?,?,1,1,'{}','2026-10-01')", (t, t, "ETF", "ETP"))
            ctx.conn.execute("INSERT INTO cusip_map VALUES(?,?,?,?,?,?)", ("C" + t, t, "ETP", "US", "{}", "x"))
        periods = quarter_ends_between(date(2021, 9, 30), date(2026, 6, 30))
        _filer(ctx.conn, "1", 10_100_000, 0.95, periods, days, closes)     # reports $9.6M, worth $10.1M: listed
        _filer(ctx.conn, "2", 9_900_000, 0.90, periods, days, closes)      # reports $8.9M: can never reach $10M
        _filer(ctx.conn, "3", 9_900_000, 0.90, periods, days, closes)      # same, but famous: always shown
    return ctx


def _investors(ctx, win="1y"):
    return json.loads(ctx.conn.execute("SELECT json FROM leaderboards WHERE grp='investors' AND win=?", (win,)).fetchone()[0])


def test_filer_reporting_9_5m_with_consistent_prices_is_still_ranked(prefilter_db, tmp_path, monkeypatch):
    seen = []
    real = pipeline._histories
    monkeypatch.setattr(pipeline, "_histories", lambda conn, ciks, *a: seen.extend(ciks) or real(conn, ciks, *a))
    m, f = _paths(tmp_path, famous=["3"])
    pipeline.build_boards(prefilter_db, m, f)
    for win in ("1y", "3y", "5y"):
        b = _investors(prefilter_db, win)
        assert [r["id"] for r in b["ranked"]] == ["1"], win
        assert b["ranked"][0]["etf_value_usd"] == pytest.approx(10_100_000, rel=1e-9)
        assert [u["id"] for u in b["unranked"]] == ["3"]
        assert b["unranked"][0]["reason"].startswith("ETF holdings below $10M")
    assert "2" not in seen and {"1", "3"} <= set(seen)              # skipped before its history is even loaded


def test_skipped_filer_still_reports_unknown_amendment(prefilter_db, tmp_path):
    with db.transaction(prefilter_db.conn):
        prefilter_db.conn.execute("INSERT INTO f13_filings VALUES('2-amend','2','Filer 2','2026-06-30','2026-08-20',"
                                  "'13F-HR/A','')")
    m, f = _paths(tmp_path)
    pipeline.build_boards(prefilter_db, m, f)
    assert prefilter_db.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='unknown-amendment-type' "
                                     "AND ref='2-amend' AND resolved=0").fetchone()[0] == 1


# ---------- detail table: boards stay small ----------

def test_stored_boards_are_small_and_details_are_kept_per_row(tmp_path):
    ctx = _ctx(tmp_path)
    _synthetic(ctx.conn, 50, 60)
    m, f = _paths(tmp_path)
    pipeline.build_boards(ctx, m, f)
    sizes = {(g, w): n for g, w, n in ctx.conn.execute("SELECT grp, win, length(json) FROM leaderboards")}
    assert max(sizes.values()) < 60_000, sizes          # was about 2.3 MB for investors/5y with details inline
    for g, w, js in ctx.conn.execute("SELECT grp, win, json FROM leaderboards"):
        b = json.loads(js)
        rows = b["ranked"] + b["unranked"]
        assert all("detail" not in r for r in rows)
        ids = {r[0] for r in ctx.conn.execute("SELECT id FROM board_details WHERE grp=? AND win=?", (g, w))}
        assert ids == {str(r["id"]) for r in rows}
    d = json.loads(ctx.conn.execute("SELECT json FROM board_details WHERE grp='investors' AND win='5y' AND id='100000'")
                   .fetchone()[0])
    assert len(d["detail"]["periods"]) == 20 and d["detail"]["series"]
    version = db.get_meta(ctx.conn, "data_version")
    assert {r[0] for r in ctx.conn.execute("SELECT DISTINCT data_version FROM board_details")} == {int(version)}
    pipeline.build_boards(ctx, m, f)                    # a rebuild replaces, never duplicates
    assert ctx.conn.execute("SELECT COUNT(*) FROM board_details WHERE grp='investors' AND win='5y'").fetchone()[0] == 60


# ---------- investor periods: stored once, not rewritten by every rebuild ----------

def _period_refs(conn) -> set[str]:
    refs = set()
    for (js,) in conn.execute("SELECT json FROM board_details WHERE grp='investors'"):
        refs |= set(json.loads(js).get("detail", {}).get("periods", []))
    return refs


def test_detail_periods_are_stored_by_content_and_read_back_in_full(tmp_path):
    """The per-quarter positions are nearly all of the detail bytes and do not change between live updates: each
    distinct period is stored once in board_periods, a detail row lists them by hash, and reading a detail puts
    every position back, in order."""
    ctx = _ctx(tmp_path)
    _synthetic(ctx.conn, 50, 60)
    m, f = _paths(tmp_path)
    out = pipeline.build_boards(ctx, m, f)
    stored = json.loads(ctx.conn.execute("SELECT json FROM board_details WHERE grp='investors' AND win='5y' "
                                         "AND id='100000'").fetchone()[0])
    assert len(stored["detail"]["periods"]) == 20 and all(isinstance(h, str) for h in stored["detail"]["periods"])
    d = pipeline.read_detail(ctx.conn, "investors", "5y", "100000")
    assert list(d["detail"]) == ["periods", "series"]
    periods = d["detail"]["periods"]
    assert [p["period"] for p in periods] == [q.isoformat() for q in quarter_ends_between(date(2021, 9, 30),
                                                                                          date(2026, 6, 30))]
    for p in periods:
        assert list(p) == ["period", "etf_value_usd", "etf_share", "url", "positions"]
        want = {c: s for c, s in ctx.conn.execute("SELECT cusip, shares FROM f13_holdings WHERE accession=?",
                                                  (f"100000-{p['period']}",))}
        assert {x["cusip"]: x["shares"] for x in p["positions"]} == want and len(p["positions"]) == 40
    n = ctx.conn.execute("SELECT COUNT(*) FROM board_periods").fetchone()[0]
    assert out["new_period_blobs"] == n > 0
    assert {r[0] for r in ctx.conn.execute("SELECT h FROM board_periods")} == _period_refs(ctx.conn)
    again = pipeline.build_boards(ctx, m, f)               # same data: nothing new to store
    assert again["new_period_blobs"] == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM board_periods").fetchone()[0] == n
    assert pipeline.read_detail(ctx.conn, "investors", "5y", "100000") == d


def test_a_changed_period_replaces_its_stored_copy(tmp_path):
    ctx = _ctx(tmp_path)
    _synthetic(ctx.conn, 50, 60)
    m, f = _paths(tmp_path)
    pipeline.build_boards(ctx, m, f)
    n = ctx.conn.execute("SELECT COUNT(*) FROM board_periods").fetchone()[0]
    with db.transaction(ctx.conn):                         # a corrected issuer name in one filing
        ctx.conn.execute("UPDATE f13_holdings SET issuer='RENAMED' WHERE accession='100000-2026-06-30'")
    out = pipeline.build_boards(ctx, m, f)
    assert out["new_period_blobs"] >= 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM board_periods").fetchone()[0] == n     # old copies removed
    assert {r[0] for r in ctx.conn.execute("SELECT h FROM board_periods")} == _period_refs(ctx.conn)
    last = pipeline.read_detail(ctx.conn, "investors", "1y", "100000")["detail"]["periods"][-1]
    assert last["period"] == "2026-06-30" and {x["name"] for x in last["positions"]} == {"RENAMED"}


def test_read_detail_passes_older_rows_through(tmp_path):
    ctx = _ctx(tmp_path)
    old = {"id": "9", "name": "N", "detail": {"periods": [{"period": "2026-06-30", "positions": []}], "series": []}}
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO board_details VALUES('investors','1y','9',?,1)", (json.dumps(old),))
    assert pipeline.read_detail(ctx.conn, "investors", "1y", "9") == old
    assert pipeline.read_detail(ctx.conn, "investors", "1y", "10") is None


# ---------- 13F stats: written by data rebuilds, read by live ones ----------

def test_only_data_rebuilds_write_the_13f_stats(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _synthetic(ctx.conn, 50, 5)
    m, f = _paths(tmp_path)
    calls, real = [], pipeline.refresh_13f_stats
    monkeypatch.setattr(pipeline, "refresh_13f_stats", lambda *a: calls.append(1) or real(*a))
    pipeline.build_boards(ctx, m, f, live=True)          # nothing stored yet: written once, even by a live rebuild
    pipeline.build_boards(ctx, m, f, live=True)
    assert calls == [1]
    pipeline.build_boards(ctx, m, f)
    assert calls == [1, 1]


def test_investors_show_their_etf_share_of_the_book(tmp_path):
    ctx = _ctx(tmp_path)
    _synthetic(ctx.conn, 50, 5)
    with db.transaction(ctx.conn):                     # filer 100000: its ETFs are half of every reported book
        ctx.conn.execute("""INSERT INTO f13_books SELECT h.accession, 2 * SUM(h.value_usd) FROM f13_holdings h
                            WHERE h.accession LIKE '100000-%' GROUP BY h.accession""")
    m, f = _paths(tmp_path)
    pipeline.build_boards(ctx, m, f)
    rows = {r["id"]: r for r in _investors(ctx, "3y")["ranked"]}
    assert rows["100000"]["etf_share"] == pytest.approx(0.5) and rows["100000"]["etf_share_avg"] == pytest.approx(0.5)
    assert rows["100001"]["etf_share"] is None and rows["100001"]["etf_share_avg"] is None
    periods = pipeline.read_detail(ctx.conn, "investors", "3y", "100000")["detail"]["periods"]
    assert [p["etf_share"] for p in periods] == pytest.approx([0.5] * len(periods))
