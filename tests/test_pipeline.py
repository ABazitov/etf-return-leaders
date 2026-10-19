import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from app import db, issues
from app.config import Config
from app.etf_check import FigiHit
from app.http import HttpError
from app.leaderboards import strip_detail
from app.members import load_terms
import app.pipeline as pipeline
from app.pipeline import (
    Ctx, _store_chart, backfill, build_boards, sync_dividends_today, sync_prices, ensure_securities, ingest_dataset, ingest_house_report, oldest_period,
    _senate_filer_is_senator, resolve_congress_tickers, tracked_tickers,
)
from app.prices import NY, parse_chart
from app.sources.house import parse_index_xml
from app.sources.sec13f import DataSet, Filing, Holding
from app.sources.senate import SenateListing

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 8, 17, 0, tzinfo=NY)
ETPS = {"SPY", "IAU", "RSP", "SCHD", "ZZZ"}


class FakeYahoo:
    def __init__(self, raises=None, trim_recent=False):
        self.raises, self.trim_recent = raises or {}, trim_recent

    def history(self, t, recent=False):
        if t in self.raises:
            raise self.raises[t]("boom")
        p = FIX / "yahoo" / f"chart_{t}_max.json"
        if not p.exists():
            raise HttpError(t, 404, "not found")
        chart = parse_chart(json.loads(p.read_text()))
        if recent and self.trim_recent:
            chart = replace(chart, rows=chart.rows[-1:])
        return chart

    def latest(self, tickers):
        return {}


class FakeFigi:
    def map_tickers(self, tickers):
        return [FigiHit(t, "ETP" if t in ETPS else "Common Stock", t, "US") for t in tickers]

    def map_cusips(self, cusips):
        known = {"464285204": "IAU", "78462F103": "SPY", "999999999": "ZZZ"}
        return [FigiHit(known[c], "ETP", known[c], "US") if c in known else None for c in cusips]


@pytest.fixture
def ctx(tmp_path):
    cfg = Config("a@b.co", "", False, False, 8765, tmp_path)
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "nasdaqtraded.txt").write_text((FIX / "reference" / "nasdaqtraded_sample.txt").read_text())
    (raw / "legislators-current.json").write_text((FIX / "reference" / "legislators_sample.json").read_text())
    (raw / "legislators-historical.json").write_text("[]")
    return Ctx(cfg, db.connect(tmp_path / "t.db"), raw, now=lambda: NOW, yahoo=FakeYahoo(), figi=FakeFigi())


def test_oldest_period():
    assert oldest_period(date(2026, 10, 8)) == date(2021, 9, 30)


def test_ensure_securities_checks_both_sources_and_stores_prices(ctx):
    assert ensure_securities(ctx, {"RSP", "MSFT"}) == {"RSP": True, "MSFT": False}
    n = ctx.conn.execute("SELECT COUNT(*) FROM prices_daily WHERE ticker='RSP'").fetchone()[0]
    assert n == 5898
    assert ctx.conn.execute("SELECT price_ok FROM securities WHERE ticker='RSP'").fetchone()[0] == 1


def test_ingest_13f_dataset_keeps_only_etf_share_rows(ctx):
    ingest_dataset(ctx, DataSet(FIX / "sec13f" / "diameter_2026q2_restatement"), date(2021, 9, 30))
    rows = ctx.conn.execute("SELECT cusip, shares FROM f13_holdings").fetchall()
    assert [(r["cusip"], r["shares"]) for r in rows] == [("464285204", 3363600.0)]
    assert ctx.conn.execute("SELECT COUNT(*) FROM f13_filings").fetchone()[0] == 1


def house_row(doc_id):
    return next(r for r in parse_index_xml((FIX / "house" / "2026FD.xml").read_bytes()) if r.doc_id == doc_id)


def test_house_report_and_ticker_resolution(ctx):
    terms = load_terms(json.loads((FIX / "reference" / "legislators_sample.json").read_text()))
    status = ingest_house_report(ctx, terms, house_row("20035410"), FIX / "house" / "20035410.pdf", "https://x/20035410.pdf")
    assert status == "parsed"
    resolve_congress_tickers(ctx)
    t = ctx.conn.execute("SELECT ticker, is_etf, is_option FROM congress_trades").fetchone()
    assert (t["ticker"], t["is_etf"], t["is_option"]) == ("RSP", 1, 0)
    m = ctx.conn.execute("SELECT party, state, chamber FROM members WHERE member_key='G000603'").fetchone()
    assert (m["party"], m["state"], m["chamber"]) == ("R", "TX", "House")


def test_scanned_house_report_recorded_as_issue(ctx):
    terms = load_terms(json.loads((FIX / "reference" / "legislators_sample.json").read_text()))
    row = replace(house_row("20035408"), doc_id="9116249")
    assert ingest_house_report(ctx, terms, row, FIX / "house" / "9116249.pdf", "https://x/9116249.pdf") == "scanned"
    kinds = [r[0] for r in ctx.conn.execute("SELECT kind FROM issues WHERE resolved=0")]
    assert kinds == ["scanned"]


def test_unmatched_member_is_kept_without_party(ctx):
    row = replace(house_row("20035410"), state_district="TX99")      # no member for that district
    assert ingest_house_report(ctx, [], row, FIX / "house" / "20035410.pdf", "https://x") == "parsed"
    m = ctx.conn.execute("SELECT name, party, state FROM members").fetchone()
    assert (m["name"], m["party"], m["state"]) == ("Brandon Gill", "?", "TX")
    assert [r[0] for r in ctx.conn.execute("SELECT kind FROM issues")] == ["member-not-matched"]


def test_build_boards_end_to_end(ctx, tmp_path):
    managers = tmp_path / "m.json"
    managers.write_text(json.dumps([{"person": "Index Person", "ticker": "SPY", "fund_name": "SPDR S&P 500 ETF Trust",
                                     "since": "2010", "source_url": "https://example.com", "evidence": "since 2010",
                                     "verified": "2026-10-08"}]))
    famous = tmp_path / "f.json"
    famous.write_text("[]")
    terms = load_terms(json.loads((FIX / "reference" / "legislators_sample.json").read_text()))
    ingest_house_report(ctx, terms, house_row("20035410"), FIX / "house" / "20035410.pdf", "https://x/20035410.pdf")
    resolve_congress_tickers(ctx)
    ingest_dataset(ctx, DataSet(FIX / "sec13f" / "diameter_2026q2_restatement"), date(2021, 9, 30))
    ensure_securities(ctx, {"SPY"})
    assert tracked_tickers(ctx.conn, managers) == {"SPY", "IAU", "RSP"}
    build_boards(ctx, managers, famous)
    boards = {(r["grp"], r["win"]): json.loads(r["json"]) for r in ctx.conn.execute("SELECT grp, win, json FROM leaderboards")}
    assert len(boards) == 9
    m = boards[("managers", "1y")]
    assert m["ranked"][0]["vs_spx"] == pytest.approx(0.0, abs=1e-9)
    assert {k: boards[("managers", k)]["ranked"][0]["beats"] for k in ("1y", "5y")} == \
        {k: {"1y": False, "3y": False, "5y": False} for k in ("1y", "5y")}     # SPY itself never beats SPY
    c = boards[("congress", "1y")]
    assert c["ranked"] == [] and c["unranked"][0]["reason"].startswith("invested on ")
    i = boards[("investors", "1y")]
    assert i["unranked"][0]["reason"] == "missing report for 2025-09-30"
    assert db.get_meta(ctx.conn, "data_version") == "1"
    assert "detail" not in strip_detail(m)["ranked"][0]


def test_senate_filer_must_be_a_senator():
    def lst(office):
        return SenateListing("Jane", "Smith", office, "https://x/ptr/abc/", "ptr", "Periodic Transaction Report for 01/01/2026",
                             date(2026, 1, 5), date(2026, 1, 1), 0)
    assert _senate_filer_is_senator(lst("Smith, Jane (Senator)")) is True
    assert _senate_filer_is_senator(lst("Smith, Jane (Candidate)")) is False
    assert _senate_filer_is_senator(lst("Staff Member")) is False


# ---------- review fixes ----------

def _track(ctx, *tickers):
    with db.transaction(ctx.conn):
        for t in tickers:
            ctx.conn.execute("INSERT OR IGNORE INTO securities(ticker, name, is_etf, price_ok, checked_at) VALUES(?,?,1,1,?)",
                             (t, t, NOW.isoformat()))
            ctx.conn.execute("INSERT INTO congress_trades(report_id, row, asset_text, ticker, is_etf, is_option) "
                             "VALUES(?,?,?,?,1,0)", (f"X{t}", 1, t, t))


def test_yahoo_keyerror_skips_one_ticker_in_dividends_today(ctx):
    _track(ctx, "RSP", "IAU")
    ctx.yahoo = FakeYahoo(raises={"IAU": KeyError})
    assert sync_dividends_today(ctx) == {"ex_dividend_today": 0}
    kinds = [(r["kind"], r["ref"]) for r in ctx.conn.execute("SELECT kind, ref FROM issues WHERE resolved=0")]
    assert ("yahoo-missing", "IAU") in kinds


def test_sync_prices_refetch_error_and_clear_ref(ctx):
    _track(ctx, "RSP", "IAU")
    ctx.yahoo = FakeYahoo(raises={"IAU": KeyError})
    assert sync_prices(ctx, full=True)["tickers"] >= 2        # SPY and RSP; IAU skipped
    assert ctx.conn.execute("SELECT COUNT(*) FROM prices_daily WHERE ticker='RSP'").fetchone()[0] > 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE ref='IAU' AND kind='yahoo-missing' AND resolved=0").fetchone()[0] == 1
    # recovery through the incremental path resolves the stale issue
    ctx.yahoo = FakeYahoo(trim_recent=True)
    sync_prices(ctx, full=False)
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE ref='IAU' AND kind='yahoo-missing' AND resolved=0").fetchone()[0] == 0


def test_sync_prices_incremental_refetch_failure_is_contained(ctx):
    _track(ctx, "RSP")
    class Flaky(FakeYahoo):
        def history(self, t, recent=False):
            if t == "RSP" and not recent:
                raise TypeError("bad")
            c = super().history(t, recent)
            return c if t != "RSP" else replace(c, rows=[replace(c.rows[-1], dividend=0.5)])
    ctx.yahoo = Flaky()
    sync_prices(ctx, full=False)
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE ref='RSP' AND kind='yahoo-missing'").fetchone()[0] == 1


def test_yahoo_failure_keeps_real_etf_as_unpriceable(ctx):
    assert ensure_securities(ctx, {"ZZZ"}) == {"ZZZ": True}
    r = ctx.conn.execute("SELECT is_etf, price_ok, yahoo_type FROM securities WHERE ticker='ZZZ'").fetchone()
    assert (r["is_etf"], r["price_ok"], r["yahoo_type"]) == (1, 0, None)
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='yahoo-missing' AND ref='ZZZ'").fetchone()[0] == 1
    f = Filing("0000000001-26-000001", "1", "Test LP", date(2026, 6, 30), date(2026, 8, 1), "13F-HR", "")
    h = Holding(f.accession, "999999999", 10.0, 1000.0, "ZZZ FUND", "ETF")
    pipeline._ingest_13f(ctx, {f.accession: f}, lambda: iter([h]), {f.accession: (1, 1000.0)})
    assert ctx.conn.execute("SELECT cusip FROM f13_holdings").fetchall()[0][0] == "999999999"


def test_corrupt_house_pdf_is_rejected_not_raised(ctx, tmp_path):
    terms = load_terms(json.loads((FIX / "reference" / "legislators_sample.json").read_text()))
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"\x00garbage not a pdf\xff" * 20)
    assert ingest_house_report(ctx, terms, house_row("20035410"), bad, "https://x/bad.pdf") == "rejected"
    r = ctx.conn.execute("SELECT status, reason FROM congress_reports").fetchone()
    assert r["status"] == "rejected" and ":" in r["reason"]
    assert [x[0] for x in ctx.conn.execute("SELECT kind FROM issues WHERE resolved=0")] == ["rejected"]


def test_sync_house_continues_after_download_failure(ctx, monkeypatch):
    rows = [replace(house_row("20035410"), doc_id=d) for d in ("1001", "1002")]
    class Client:
        def __init__(self, raw): pass
        def index(self, y): return rows
        def pdf(self, y, doc_id):
            if doc_id == "1001":
                raise HttpError("u", 500, "down")
            return FIX / "house" / "20035410.pdf"
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    out = pipeline.sync_house(ctx, [2026])
    statuses = {r["report_id"]: r["status"] for r in ctx.conn.execute("SELECT report_id, status FROM congress_reports")}
    assert statuses == {"H1002": "parsed"} and out == {"fetch-failed": 1, "parsed": 1}   # 500 is transient: retried later


def _spy_boards(ctx, tmp_path):
    managers, famous = tmp_path / "m.json", tmp_path / "f.json"
    managers.write_text("[]")
    famous.write_text("[]")
    return managers, famous


def test_build_boards_applies_senate_supersede(ctx, tmp_path):
    managers, famous = _spy_boards(ctx, tmp_path)
    ensure_securities(ctx, {"SPY"})
    with db.transaction(ctx.conn):
        for rid, n in (("S1", ""), ("S2", " (Amendment 1)")):
            ctx.conn.execute("INSERT INTO congress_reports(report_id, chamber, filer_name, member_key, filed, url, status, reason, title) "
                             "VALUES(?,?,?,?,?,?,?,?,?)",
                             (rid, "senate", "A B", "K1", "2026-01-05", "u", "parsed", "",
                              f"Periodic Transaction Report for 01/01/2026{n}"))
    build_boards(ctx, managers, famous)
    st = {r["report_id"]: r["status"] for r in ctx.conn.execute("SELECT report_id, status FROM congress_reports")}
    assert st == {"S1": "superseded", "S2": "parsed"}


def test_famous_filer_without_filings_is_listed_unranked(ctx, tmp_path):
    managers, famous = _spy_boards(ctx, tmp_path)
    famous.write_text(json.dumps([{"cik": "1067983", "filer_name": "BERKSHIRE HATHAWAY INC", "person": "Warren Buffett",
                                   "role": "CEO", "source_url": "https://example.com", "evidence": "x",
                                   "verified": "2026-10-08"}]))
    ensure_securities(ctx, {"SPY"})
    build_boards(ctx, managers, famous)
    b = json.loads(ctx.conn.execute("SELECT json FROM leaderboards WHERE grp='investors' AND win='1y'").fetchone()[0])
    assert any("BERKSHIRE" in json.dumps(u) and u["reason"].startswith("missing report for") for u in b["unranked"])


def test_totals_single_pass():
    from app.pipeline import _totals
    hs = [Holding("A", "c1", 1, 10.0, "", ""), Holding("A", "c2", 1, 5.0, "", ""), Holding("B", "c1", 1, 7.0, "", "")]
    assert _totals(hs) == {"A": (2, 15.0), "B": (1, 7.0)}


def test_build_boards_requires_spy_history(ctx, tmp_path):
    managers, famous = _spy_boards(ctx, tmp_path)
    with pytest.raises(RuntimeError, match="SPY price history missing"):
        build_boards(ctx, managers, famous)
    assert ctx.conn.execute("SELECT COUNT(*) FROM leaderboards").fetchone()[0] == 0


def test_empty_chart_x1_message(ctx):
    chart = replace(FakeYahoo().history("RSP"), rows=[])
    _store_chart(ctx, chart)
    msg = ctx.conn.execute("SELECT message FROM issues WHERE kind='x1-dividend-check'").fetchone()[0]
    assert "no adjusted prices to check" in msg and "inf" not in msg


def test_backfill_skips_full_price_refetch(ctx, monkeypatch):
    calls = []
    for name in ("refresh_reference", "sync_house", "sync_senate", "resolve_congress_tickers", "sync_13f_datasets",
                 "sync_13f_new", "build_boards"):
        monkeypatch.setattr(pipeline, name, lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "ensure_securities", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "sync_prices", lambda c, full=False: calls.append(full) or {})
    backfill(ctx)
    assert calls == [False]


# ---------- review fixes round 2 ----------

def test_incremental_sync_keeps_x1_issue_open(ctx):
    _track(ctx, "IAU")
    with db.transaction(ctx.conn):
        pipeline.issues.record(ctx.conn, "prices", "x1-dividend-check", "IAU", "x1")
        pipeline.issues.record(ctx.conn, "prices", "yahoo-missing", "IAU", "gone")
    ctx.yahoo = FakeYahoo(trim_recent=True)
    sync_prices(ctx, full=False)
    st = {r["kind"]: r["resolved"] for r in ctx.conn.execute("SELECT kind, resolved FROM issues WHERE ref='IAU'")}
    assert st == {"x1-dividend-check": 0, "yahoo-missing": 1}


def _house_client(monkeypatch, rows, status):
    class Client:
        def __init__(self, raw): pass
        def index(self, y): return rows
        def pdf(self, y, doc_id): raise HttpError("https://x/p.pdf", status, "err")
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)


def test_transient_house_failure_is_retried_not_recorded(ctx, monkeypatch):
    _house_client(monkeypatch, [replace(house_row("20035410"), doc_id="1001")], 503)
    pipeline.sync_house(ctx, [2026])
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_reports").fetchone()[0] == 0
    r = ctx.conn.execute("SELECT kind, ref, url, resolved FROM issues").fetchall()
    assert [(x["kind"], x["ref"], x["resolved"]) for x in r] == [("fetch-failed", "H1001", 0)] and r[0]["url"]


def test_permanent_house_failure_is_rejected(ctx, monkeypatch):
    _house_client(monkeypatch, [replace(house_row("20035410"), doc_id="1001")], 404)
    pipeline.sync_house(ctx, [2026])
    assert ctx.conn.execute("SELECT status FROM congress_reports").fetchone()[0] == "rejected"


def test_unexpected_errors_are_not_swallowed(ctx, monkeypatch):
    class Client:
        def __init__(self, raw): pass
        def index(self, y): return [replace(house_row("20035410"), doc_id="1001")]
        def pdf(self, y, doc_id): raise KeyError("bug")
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    with pytest.raises(KeyError):
        pipeline.sync_house(ctx, [2026])


def _failed_yahoo_security(ctx, instrument_type):
    ensure_securities(ctx, {"ZZZ"})                      # Yahoo fails: is_etf=1, price_ok=0, yahoo_type NULL
    chart = replace(FakeYahoo().history("RSP"), ticker="ZZZ", instrument_type=instrument_type)
    _store_chart(ctx, chart)
    return ctx.conn.execute("SELECT is_etf, price_ok, yahoo_type FROM securities WHERE ticker='ZZZ'").fetchone()


def test_store_chart_applies_e1_after_earlier_yahoo_failure(ctx):
    r = _failed_yahoo_security(ctx, "ETF")
    assert (r["is_etf"], r["price_ok"], r["yahoo_type"]) == (1, 1, "ETF")


def test_store_chart_demotes_non_etf_and_records_x3(ctx):
    r = _failed_yahoo_security(ctx, "EQUITY")
    assert (r["is_etf"], r["price_ok"], r["yahoo_type"]) == (0, 0, "EQUITY")
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='x3-identity' AND ref='ZZZ' AND resolved=0").fetchone()[0] == 1


def test_sync_13f_new_ingests_every_200_downloads(ctx, monkeypatch):
    with db.transaction(ctx.conn):
        db.set_meta(ctx.conn, "13f-idx-from", NOW.date().isoformat())
    accs = [f"0000000001-26-{i:06d}" for i in range(250)]
    n_dl = {"n": 0}
    def sec(url):
        if "getcurrent" in url:
            return b"HR" if "type=13F-HR&" in url else b"HRA"
        if url.endswith("index.json"):
            return json.dumps({"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "info.xml"}]}}).encode()
        if url.endswith("info.xml"):
            n_dl["n"] += 1
            if n_dl["n"] > 230:
                raise HttpError(url, 500, "crash")
        return b"informationTable"
    ctx.sec = sec
    sf = pipeline.sec13f
    monkeypatch.setattr(sf, "parse_current_atom", lambda b: [("13F-HR", "1", a, date(2026, 10, 7)) for a in accs] if b == b"HR" else [])
    monkeypatch.setattr(sf, "parse_primary_doc", lambda body, acc, cik, filed, form: Filing(acc, cik, "F", date(2026, 6, 30), filed, form, ""))
    monkeypatch.setattr(sf, "parse_infotable_xml", lambda body, f: [Holding(f.accession, "c", 1, 1.0, "", "")])
    calls = []
    monkeypatch.setattr(pipeline, "_ingest_13f", lambda c, filings, fn, totals: calls.append(len(filings)) or {"filings": len(filings)})
    with pytest.raises(HttpError):
        pipeline.sync_13f_new(ctx)
    assert calls == [200]
    assert db.get_meta(ctx.conn, "13f-idx-from") == NOW.date().isoformat()


def test_sync_13f_new_skips_unreadable_filings_and_goes_on(ctx, monkeypatch):
    """A filing whose index.json answers 404 and one whose primary_doc.xml is broken each get a Data issue; the
    readable filing is still stored and the run finishes (a server error still stops it, see the test above)."""
    with db.transaction(ctx.conn):
        db.set_meta(ctx.conn, "13f-idx-from", NOW.date().isoformat())
    gone, broken, good = "0000000002-26-000002", "0000000003-26-000003", "0000000001-26-000001"

    def sec(url):
        if "getcurrent" in url:
            return b"HR" if "type=13F-HR&" in url else b"HRA"
        if "/000000000226000002/" in url:
            raise HttpError(url, 404, "Not Found")
        if url.endswith("index.json"):
            return json.dumps({"directory": {"item": [{"name": "primary_doc.xml"}, {"name": "info.xml"}]}}).encode()
        if url.endswith("primary_doc.xml"):
            if "/000000000326000003/" in url:
                return b"<edgarSubmission><headerData"
            return (FIX / "sec13f" / "diameter_primary_doc.xml").read_bytes()
        return (FIX / "sec13f" / "diameter_infotable.xml").read_bytes()
    ctx.sec = sec
    monkeypatch.setattr(pipeline.sec13f, "parse_current_atom",
                        lambda b: [("13F-HR", "1", a, date(2026, 10, 7)) for a in (gone, broken, good)] if b == b"HR" else [])
    calls = []
    monkeypatch.setattr(pipeline, "_ingest_13f", lambda c, filings, fn, totals: calls.append(sorted(filings)) or {"filings": len(filings)})
    assert pipeline.sync_13f_new(ctx) == {"filings": 1}
    assert calls == [[good]]
    got = sorted(r[0] for r in ctx.conn.execute("SELECT ref FROM issues WHERE kind='filing-unreadable' AND resolved=0"))
    assert got == [gone, broken]


def test_unidentified_cusip_issue_ref_is_unique_per_batch(ctx):
    for i in (1, 2):
        f = Filing(f"0000000001-26-00000{i}", "1", "T", date(2026, 6, 30), date(2026, 8, 1), "13F-HR", "")
        h = Holding(f.accession, f"88888888{i}", 1.0, 1.0, "X", "")
        pipeline._ingest_13f(ctx, {f.accession: f}, lambda h=h: iter([h]), {f.accession: (1, 1.0)})
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='unidentified-cusips'").fetchone()[0] == 2


# ---------- review fixes round 3 ----------

def test_house_transient_failure_in_old_year_is_retried_by_default_sync(ctx, monkeypatch):
    row = replace(house_row("20035410"), doc_id="1001")
    state = {"fail": True, "years": []}
    class Client:
        def __init__(self, raw): pass
        def index(self, y):
            state["years"].append(y)
            return [row] if y == 2014 else []
        def pdf(self, y, doc_id):
            if state["fail"]:
                raise HttpError("u", 503, "down")
            return FIX / "house" / "20035410.pdf"
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    pipeline.sync_house(ctx, [2014])
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_reports").fetchone()[0] == 0
    state["fail"], state["years"] = False, []
    pipeline.sync_house(ctx)                                  # default years: 2025, 2026 plus the failed 2014
    assert 2014 in state["years"]
    assert ctx.conn.execute("SELECT status FROM congress_reports WHERE report_id='H1001'").fetchone()[0] == "parsed"


def test_senate_transient_failure_moves_senate_from_back_and_is_retried(ctx, monkeypatch):
    ctx = replace(ctx, cfg=replace(ctx.cfg, senate_enabled=True))
    lst = SenateListing("Jane", "Smith", "Smith, Jane (Senator)", "https://x/ptr/abc/", "ptr",
                        "Periodic Transaction Report for 03/01/2014", date(2014, 3, 5), date(2014, 3, 1), 0)
    html = (FIX / "senate" / "ptr_0541be4f-4f96-4d84-8d6f-d349f218bb2a.html").read_text()
    state = {"fail": True, "since": []}
    class Client:
        def __init__(self, visible=False): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def search(self, since):
            state["since"].append(since)
            return [lst] if since <= lst.filed else []
        def page(self, url):
            if state["fail"]:
                raise HttpError(url, 503, "down")
            return html
    monkeypatch.setattr(pipeline.senate_src, "SenateClient", Client)
    pipeline.sync_senate(ctx)
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_reports").fetchone()[0] == 0
    assert db.get_meta(ctx.conn, "senate-from") == "2014-03-05"
    state["fail"] = False
    pipeline.sync_senate(ctx)
    assert state["since"][-1] <= date(2014, 3, 5)
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_reports WHERE report_id='Sabc'").fetchone()[0] == 1
    assert db.get_meta(ctx.conn, "senate-from") == NOW.date().isoformat()


def test_incremental_sync_runs_e1_for_unresolved_etp(ctx):
    ensure_securities(ctx, {"ZZZ"})
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO congress_trades(report_id, row, ticker, is_etf, is_option) VALUES('Y',1,'ZZZ',1,0)")
    ZZZ_CHART = replace(FakeYahoo().history("RSP"), ticker="ZZZ")
    class Y(FakeYahoo):
        def history(self, t, recent=False):
            if t == "ZZZ":
                return replace(ZZZ_CHART, rows=ZZZ_CHART.rows[-1:]) if recent else ZZZ_CHART
            return super().history(t, recent)
    ctx.yahoo = Y()
    sync_prices(ctx, full=False)
    r = ctx.conn.execute("SELECT is_etf, price_ok, yahoo_type FROM securities WHERE ticker='ZZZ'").fetchone()
    assert (r["is_etf"], r["price_ok"], r["yahoo_type"]) == (1, 1, "ETF")


def test_demotion_removes_trades_and_holdings(ctx):
    ensure_securities(ctx, {"ZZZ"})
    f = Filing("0000000001-26-000001", "1", "T", date(2026, 6, 30), date(2026, 8, 1), "13F-HR", "")
    pipeline._ingest_13f(ctx, {f.accession: f}, lambda: iter([Holding(f.accession, "999999999", 1.0, 1.0, "ZZZ", "ETF")]),
                         {f.accession: (1, 1.0)})
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO congress_trades(report_id, row, ticker, is_etf, is_option) VALUES('X',1,'ZZZ',1,0)")
    assert ctx.conn.execute("SELECT COUNT(*) FROM f13_holdings").fetchone()[0] == 1
    _store_chart(ctx, replace(FakeYahoo().history("RSP"), ticker="ZZZ", instrument_type="EQUITY"))
    assert ctx.conn.execute("SELECT COUNT(*) FROM f13_holdings").fetchone()[0] == 0
    assert ctx.conn.execute("SELECT is_etf FROM congress_trades WHERE report_id='X'").fetchone()[0] == 0


def _arkk_chart():
    return parse_chart(json.loads((FIX / "spotcheck" / "chart_ARKK.json").read_text()))


def _stored(ctx, day):
    return ctx.conn.execute("SELECT dividend FROM prices_daily WHERE ticker='ARKK' AND day=?", (day,)).fetchone()[0]


def test_store_chart_applies_dividend_correction_and_flags_x6(ctx):
    ctx.conn.execute("INSERT INTO securities(ticker, name, yahoo_type, figi_type, is_etf, price_ok, evidence, checked_at) "
                     "VALUES('ARKK','ARK','ETF','ETP',1,1,'{}','2026-10-01')")
    ctx.conn.commit()
    assert _store_chart(ctx, _arkk_chart()) is True          # X1 ran on Yahoo's own (uncorrected) rows
    assert _stored(ctx, "2023-09-08") == 0.0
    assert _stored(ctx, "2021-12-29") == 0.783
    msgs = {r["kind"]: r["message"] for r in ctx.conn.execute("SELECT kind, message FROM issues WHERE ref='ARKK' AND resolved=0")}
    assert "2023-09-08" in msgs["dividend-corrected"] and "sec.gov" in msgs["dividend-corrected"]
    assert "2021-12-29" in msgs["x6-irregular-dividend"] and "2023-09-08" not in msgs["x6-irregular-dividend"]
    assert msgs["x6-irregular-dividend"].endswith("verify against the fund's own reports; add a dividend correction if wrong")
    assert "x1-dividend-check" not in msgs


def test_sync_prices_incremental_applies_corrections(ctx):
    _track(ctx, "ARKK")
    full = _arkk_chart()
    class Y(FakeYahoo):
        def history(self, t, recent=False):
            return full if t == "ARKK" and not recent else replace(full, rows=[r for r in full.rows if r.day >= date(2023, 9, 1)][:20]) if t == "ARKK" else super().history(t, recent)
    ctx.yahoo = Y()
    sync_prices(ctx, full=False)           # recent rows carry the 2023-09-08 dividend -> refetch path stores corrected rows
    assert _stored(ctx, "2023-09-08") == 0.0


# ---------- final review: backfill fetches SPY's full history (item 3) ----------

def test_backfill_fund_managers_step_fetches_full_spy_history(ctx, monkeypatch):
    for name in ("refresh_reference", "sync_house", "sync_senate", "resolve_congress_tickers", "sync_13f_datasets",
                 "sync_13f_new", "build_boards", "sync_prices"):
        monkeypatch.setattr(pipeline, name, lambda *a, **k: {})
    ensure_securities(ctx, {"SPY"})
    with db.transaction(ctx.conn):            # fresh securities row, but only a few recent SPY rows stored
        ctx.conn.execute("DELETE FROM prices_daily WHERE ticker='SPY' AND day < '2026-09-01'")
    assert ctx.conn.execute("SELECT COUNT(*) FROM prices_daily WHERE ticker='SPY'").fetchone()[0] < 2000
    backfill(ctx)
    assert ctx.conn.execute("SELECT COUNT(*) FROM prices_daily WHERE ticker='SPY'").fetchone()[0] > 2000


def test_failed_house_backfill_makes_the_hourly_job_catch_up_all_years(ctx, monkeypatch):
    for name in ("refresh_reference", "sync_senate", "resolve_congress_tickers", "sync_13f_datasets",
                 "sync_13f_new", "build_boards", "sync_prices", "_backfill_fund_managers"):
        monkeypatch.setattr(pipeline, name, lambda *a, **k: {})
    years = []

    class Client:
        fail = True
        def __init__(self, raw): pass
        def index(self, y):
            years.append(y)
            if Client.fail:
                raise HttpError("u", 503, "down")
            return []
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    backfill(ctx)
    assert db.source_status(ctx.conn)["house"]["last_error"].startswith("HttpError")
    Client.fail, years[:] = False, []
    pipeline.sync_house(ctx)                     # the hourly job, default years
    assert years == list(range(pipeline.FIRST_YEAR, NOW.year + 1))
    years.clear()
    pipeline.sync_house(ctx)
    assert years == [NOW.year - 1, NOW.year]


# ---------- final review items 9, 10, 12, 13 ----------

def test_sync_house_resolves_tickers_when_something_new_was_parsed(ctx, monkeypatch):
    rows = [replace(house_row("20035410"), doc_id="1001")]

    class Client:
        def __init__(self, raw): pass
        def index(self, y): return rows
        def pdf(self, y, doc_id): return FIX / "house" / "20035410.pdf"
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    calls = []
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: calls.append(1) or {})
    pipeline.sync_house(ctx, [2026])
    assert calls == [1]
    pipeline.sync_house(ctx, [2026])                 # nothing new
    assert calls == [1]


def test_sync_senate_resolves_tickers_when_something_new_was_parsed(ctx, monkeypatch):
    ctx = replace(ctx, cfg=replace(ctx.cfg, senate_enabled=True))
    lst = SenateListing("Jane", "Smith", "Smith, Jane (Senator)", "https://x/ptr/abc/", "ptr",
                        "Periodic Transaction Report for 03/01/2026", date(2026, 3, 5), date(2026, 3, 1), 0)
    html = (FIX / "senate" / "ptr_0541be4f-4f96-4d84-8d6f-d349f218bb2a.html").read_text()

    class Client:
        def __init__(self, visible=False): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def search(self, since): return [lst]
        def page(self, url): return html
    monkeypatch.setattr(pipeline.senate_src, "SenateClient", Client)
    calls = []
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: calls.append(1) or {})
    pipeline.sync_senate(ctx)
    assert calls == [1]
    pipeline.sync_senate(ctx)
    assert calls == [1]


def test_house_row_without_filing_date_is_an_issue_not_a_crash(ctx, monkeypatch):
    rows = [replace(house_row("20035410"), doc_id="1001", filed=None), replace(house_row("20035410"), doc_id="1002")]

    class Client:
        def __init__(self, raw): pass
        def index(self, y): return rows
        def pdf(self, y, doc_id): return FIX / "house" / "20035410.pdf"
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    out = pipeline.sync_house(ctx, [2026])
    assert out.get("parsed") == 1
    r = ctx.conn.execute("SELECT source, kind, url FROM issues WHERE ref='H1001' AND resolved=0").fetchall()
    assert [(x["source"], x["kind"]) for x in r] == [("house", "missing-filing-date")] and r[0]["url"]


def test_unmatched_dividend_correction_is_reported(ctx):
    ctx.conn.execute("INSERT INTO securities(ticker, name, yahoo_type, figi_type, is_etf, price_ok, evidence, checked_at) "
                     "VALUES('ARKK','ARK','ETF','ETP',1,1,'{}','2026-10-01')")
    chart = _arkk_chart()
    _store_chart(ctx, replace(chart, rows=[r for r in chart.rows if r.day != date(2023, 9, 8)]))
    msg = ctx.conn.execute("SELECT message FROM issues WHERE kind='dividend-correction-unmatched' AND ref='ARKK' "
                           "AND resolved=0").fetchone()
    assert msg is not None and "2023-09-08" in msg[0]
    _store_chart(ctx, chart)                          # matched again: the issue resolves
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='dividend-correction-unmatched' "
                            "AND resolved=0").fetchone()[0] == 0


def test_corrections_verified_check_uses_the_context_clock(ctx, tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"ticker": "ARKK", "ex_date": "2023-09-08", "action": "remove",
                                 "source_url": "https://x", "evidence": "e", "verified": "2099-01-01"}]))
    later = replace(ctx, now=lambda: datetime(2099, 6, 1, 12, 0, tzinfo=NY))
    assert [c.ex_date for c in pipeline._corrections_for(later, "ARKK", path)] == [date(2023, 9, 8)]


def test_sync_prices_raises_when_every_ticker_failed(ctx):
    _track(ctx, "RSP")
    ctx.yahoo = FakeYahoo(raises={"SPY": KeyError, "RSP": HttpError})
    with pytest.raises(RuntimeError, match="all Yahoo requests failed"):
        sync_prices(ctx, full=False)
    refs = {r[0] for r in ctx.conn.execute("SELECT ref FROM issues WHERE kind='yahoo-missing' AND resolved=0")}
    assert {"SPY", "RSP"} <= refs                    # each failure is still recorded (plus the curated managers' ETFs)


def test_dividends_today_raises_when_every_ticker_failed(ctx):
    _track(ctx, "RSP")
    ctx.yahoo = FakeYahoo(raises={"SPY": KeyError, "RSP": KeyError})
    with pytest.raises(RuntimeError, match="all Yahoo requests failed"):
        sync_dividends_today(ctx)
    missing = {r[0] for r in ctx.conn.execute("SELECT ref FROM issues WHERE kind='yahoo-missing' AND resolved=0")}
    assert {"SPY", "RSP"} <= missing               # with the curated funds, which have no fixtures here
    assert db.get_meta(ctx.conn, f"div-today:{NOW.date().isoformat()}") is None


def test_dividends_today_stores_everything_in_one_write(ctx, monkeypatch):
    """The job reads every tracked ticker (most of an hour); a write per failed ticker would wait on each board
    rebuild in progress, so everything is stored in one transaction at the end."""
    _track(ctx, "RSP", "IAU")
    ctx.yahoo = FakeYahoo(raises={"IAU": KeyError})
    real, calls = db.transaction, []

    def counted(conn):
        calls.append(conn)
        return real(conn)

    monkeypatch.setattr(db, "transaction", counted)
    assert sync_dividends_today(ctx) == {"ex_dividend_today": 0}
    assert len(calls) == 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE ref='IAU' AND kind='yahoo-missing' AND resolved=0"
                            ).fetchone()[0] == 1
    assert db.get_meta(ctx.conn, f"div-today:{NOW.date().isoformat()}") == "{}"


def test_histories_merge_new_holdings_amendments_into_positions(ctx):
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO securities(ticker, name, is_etf, price_ok, checked_at) VALUES('IAU','IAU',1,1,'x')")
        ctx.conn.execute("INSERT INTO cusip_map VALUES('464285204','IAU','ETP','US','{}','x')")
        for acc, form, amend, filed in (("A1", "13F-HR", None, "2026-08-01"), ("A2", "13F-HR/A", "NEW HOLDINGS", "2026-08-05")):
            ctx.conn.execute("INSERT INTO f13_filings VALUES(?,?,?,?,?,?,?)", (acc, "9", "F", "2026-06-30", filed, form, amend))
        ctx.conn.executemany("INSERT INTO f13_holdings VALUES(?,?,?,?,?,?)", [
            ("A1", "464285204", 10.0, 100.0, "ISHARES GOLD", "ETF"), ("A2", "464285204", 5.0, 50.0, "AMENDED NAME", "ETF"),
            ("A2", "888888888", 1.0, 7.0, None, "ETF")])
    (h,), unknown = pipeline._histories(ctx.conn, ["9"])
    pos = {p.cusip: p for p in h.periods[date(2026, 6, 30)]}
    assert (pos["464285204"].ticker, pos["464285204"].shares, pos["464285204"].reported_usd, pos["464285204"].name) == \
        ("IAU", 15.0, 150.0, "ISHARES GOLD")
    assert (pos["888888888"].ticker, pos["888888888"].reported_usd, pos["888888888"].name) == (None, 7.0, "")
    assert h.accessions[date(2026, 6, 30)] == "A2" and unknown == []


# ---------- House reports in the pre-2022 layout ----------

OLD_HOUSE = FIX / "house" / "old"


def _old_row(doc_id, last, first, sd, filed):
    from app.sources.house import IndexRow
    return IndexRow(doc_id, "Hon.", first, last, "", "P", sd, filed.year, filed)


def test_option_description_rule():
    for d in ("PUT 2/22/2014 $525 (100SHS)", "CaLL 2/22/2014 $39 (100SHS)", "PUT MAY 17, 2014 $97.50 (100SHS)",
              "Call Option, $177.50, Exp. 4/5/19", "Call options; Strike price $50", "Put Facebook Inc $75 Exp 04/19/14",
              "Purchase of 50 Options", "CoP Jan 15 2016 70.0 Call", "PuT Puma Biotechnology $275 Exp 12/20/14",
              "PUT", "CaLL", "CaLL aSSIgNMENT", "Put assignment"):
        assert pipeline._option_description(d), d
    for d in ("", "Private equity fund capital call", "Capital Call",
              'Pro-rata refund of prior capital calls (Not a sale--the Congressional PTR system does not have an "Other"'
              ' transaction type.)', "Callable bond redeemed", "Purchase",
              "Exercise of stock options", "Exercise of stock option shares", "Call 01/30/23", "Call 05/15/23@$100.00",
              "Redeemed Full Call Payout 4.25%; 10/15/20", "Capital Call-- $7k for laurel Mountain Energy llC Investment",            # call dates of preferred shares / bonds
              "Depository Shares 1/25 PFD Series D CPN 5.350% DTD 4/30/13 FC 11/15/13 Call 05/15/23@100.00"):
        assert not pipeline._option_description(d), d


def test_old_layout_options_are_flagged_from_code_and_description(ctx):
    hanna = _old_row("20000320", "Hanna", "Richard L.", "NY22", date(2014, 2, 5))
    assert ingest_house_report(ctx, [], hanna, OLD_HOUSE / "2014-20000320.pdf", "https://x/2014/20000320.pdf") == "parsed"
    langevin = _old_row("20011457", "Langevin", "James R.", "RI02", date(2019, 4, 8))
    assert ingest_house_report(ctx, [], langevin, OLD_HOUSE / "2019-20011457.pdf", "https://x/2019/20011457.pdf") == "parsed"
    flagged = {(r["report_id"], r["asset_text"]) for r in ctx.conn.execute(
        "SELECT report_id, asset_text FROM congress_trades WHERE is_option=1")}
    assert flagged == {("H20000320", "gap, Inc. (GPS)"), ("H20000320", "PP&L Corporation (PPL)"),
                       ("H20000320", "Scripps Networks Interactive, Inc Common Class a (SNI)"),
                       ("H20000320", "United Parcel Service, Inc. (UPS)"),
                       ("H20011457", "alibaba group Holding Limited american Depositary shares each representing one "
                                     "Ordinary share (BABA)"),
                       ("H20011457", "Tesla, Inc. (TSLA)")}
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_trades WHERE is_option=0").fetchone()[0] == 11


def test_option_named_in_asset_text_is_flagged(ctx, monkeypatch):
    from app.sources.house import HouseReport, RawTrade
    cases = [("Apple Inc. (AAPL) Call Options", "", 1), ("S&P 500 aUg 19 1725 PUT", "PUT", 1),
             ("ISHaRES 20 YEaR TREaS boND 134 CaLL", "", 1),         # seen in 2016 reports, no asset code
             ("ISHaRES TLT JUL 1 138 PUT aSSIgNMENT", "CaLL aSSIgNMENT", 1),   # an option assignment
             ("PUT/XSP @ 195 EXP 09/16/2016 - CBoE MINI SPX INDEX", "", 1),
             ("PUX/XSP @ 188 EXP 09/16/2016 - CBoE MINI SPX INDEX", "", 1),
             ("SPDR S&P 500 ETF TRUST @ 200.00 EXP 12/20/2019", "", 1),
             ("HEWLETT PACKARD Co CALL 02.600% 091517", "", 0),      # callable bonds
             ("LENNAR CoRP CALL@MW+50BP 04.500% 061519", "", 0), ("Apple Inc. (AAPL)", "", 0)]
    trades = [RawTrade(i + 1, "self", a, "", "P", date(2016, 1, 2), date(2016, 1, 5), "$1,001 - $15,000", d)
              for i, (a, d, _) in enumerate(cases)]
    monkeypatch.setattr(pipeline.house_src, "parse_pdf", lambda p: HouseReport("x", "Member", "TX26", "1", trades))
    ingest_house_report(ctx, [], house_row("20035410"), FIX / "house" / "20035410.pdf", "https://x")
    got = {r[0]: r[1] for r in ctx.conn.execute("SELECT asset_text, is_option FROM congress_trades")}
    assert got == {a: want for a, _, want in cases}


def _stored_old_rejection(ctx, monkeypatch, reason="missing header field Name"):
    """A Langevin 2018 report as an older version stored it: rejected, with its PDF and index cached."""
    from app.sources.errors import ParseRejected
    d = ctx.raw / "house" / "2018"
    d.mkdir(parents=True, exist_ok=True)
    (d / "20008973.pdf").write_bytes((OLD_HOUSE / "2018-20008973.pdf").read_bytes())
    (ctx.raw / "house" / "2018FD.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><FinancialDisclosure><Member><Prefix>Hon.</Prefix><Last>Langevin</Last>'
        '<First>James R.</First><Suffix /><FilingType>P</FilingType><StateDst>RI02</StateDst><Year>2018</Year>'
        '<FilingDate>2/15/2018</FilingDate><DocID>20008973</DocID></Member></FinancialDisclosure>')
    row = _old_row("20008973", "Langevin", "James R.", "RI02", date(2018, 2, 15))

    def reject(path):
        raise ParseRejected(reason)
    with monkeypatch.context() as m:
        m.setattr(pipeline.house_src, "parse_pdf", reject)
        url = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2018/20008973.pdf"
        assert ingest_house_report(ctx, [], row, d / "20008973.pdf", url) == "rejected"


def test_reprocess_rejected_old_layout_house_reports(ctx, monkeypatch):
    _stored_old_rejection(ctx, monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: calls.append(1) or {})
    assert pipeline.reprocess_rejected_house(ctx) == {"parsed": 1}
    r = ctx.conn.execute("SELECT status, reason FROM congress_reports WHERE report_id='H20008973'").fetchone()
    assert (r["status"], r["reason"]) == ("parsed", "")
    t = ctx.conn.execute("SELECT owner, asset_text, tx_type, tx_date, amount_text FROM congress_trades").fetchall()
    assert [tuple(x) for x in t] == [("self", "alibaba group Holding Limited american Depositary shares each "
                                      "representing one Ordinary share (BABA)", "S", "2018-02-14", "$100,001 - $250,000")]
    open_kinds = [x[0] for x in ctx.conn.execute("SELECT kind FROM issues WHERE ref='H20008973' AND resolved=0")]
    assert open_kinds == ["member-not-matched"]          # the "rejected" issue is resolved (no terms in this test)
    assert calls == [1]
    assert pipeline.reprocess_rejected_house(ctx) == {}          # nothing left to redo
    assert calls == [1]


def test_reprocess_leaves_other_rejections_alone(ctx, monkeypatch):
    _stored_old_rejection(ctx, monkeypatch, reason="found 2 asset codes but parsed 1 transactions")
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    assert pipeline.reprocess_rejected_house(ctx) == {}
    assert ctx.conn.execute("SELECT status FROM congress_reports").fetchone()[0] == "rejected"


def test_reprocess_without_cached_pdf_keeps_the_rejection(ctx, monkeypatch):
    _stored_old_rejection(ctx, monkeypatch)
    (ctx.raw / "house" / "2018" / "20008973.pdf").unlink()
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    assert pipeline.reprocess_rejected_house(ctx) == {"missing-pdf": 1}
    assert ctx.conn.execute("SELECT status FROM congress_reports").fetchone()[0] == "rejected"


def test_sync_house_reprocesses_once_per_parser_upgrade(ctx, monkeypatch):
    _stored_old_rejection(ctx, monkeypatch)

    class Client:
        def __init__(self, raw): pass
        def index(self, y): return []
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    calls = []
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: calls.append(1) or {})
    pipeline.sync_house(ctx, [2026])
    assert ctx.conn.execute("SELECT status FROM congress_reports").fetchone()[0] == "parsed"
    assert db.get_meta(ctx.conn, "house-parser-version") == "5" and calls == [1]
    ctx.conn.execute("DELETE FROM congress_reports")
    _stored_old_rejection(ctx, monkeypatch)
    pipeline.sync_house(ctx, [2026])                              # already upgraded: not redone
    assert ctx.conn.execute("SELECT status FROM congress_reports").fetchone()[0] == "rejected"


# ---------- C9: Deleted and Amended rows ----------

def test_ingested_house_rows_keep_their_filing_status(ctx):
    row = _old_row("20001268", "Grijalva", "Raúl M.", "AZ03", date(2014, 7, 9))
    ingest_house_report(ctx, [], row, OLD_HOUSE / "2014-20001268.pdf", "https://x/2014/20001268.pdf")
    assert [r[0] for r in ctx.conn.execute("SELECT filing_status FROM congress_trades ORDER BY row")] == [
        "Deleted", "Deleted"]


def test_real_deleted_rows_cancel_their_original_trades(ctx):
    """Grijalva's 2014 report 20001268 deletes both rows of 20001226 (filed 8 days earlier)."""
    for doc, filed in (("20001226", date(2014, 7, 1)), ("20001268", date(2014, 7, 9))):
        row = _old_row(doc, "Grijalva", "Raúl M.", "AZ03", filed)
        assert ingest_house_report(ctx, [], row, OLD_HOUSE / f"2014-{doc}.pdf", f"https://x/2014/{doc}.pdf") == "parsed"
    ctx.conn.execute("UPDATE congress_trades SET is_etf=1, ticker=CASE WHEN asset_text LIKE '%(INF)' THEN 'INF' "
                     "ELSE 'OPY' END")                          # stand-ins: treat both as ETFs for this test
    members, trades, problems = pipeline._congress_inputs(ctx.conn)
    assert trades.get("H-AZ03-Grijalva", []) == [] and problems == []
    ctx.conn.execute("DELETE FROM congress_trades WHERE report_id='H20001268'")
    _, trades, _ = pipeline._congress_inputs(ctx.conn)
    assert sorted(t.ticker for t in trades["H-AZ03-Grijalva"]) == ["INF", "OPY"]


def _c9_report(ctx, rid, filed, rows, member="M1"):
    ctx.conn.execute("INSERT OR IGNORE INTO members(member_key, name, chamber, party, state) "
                     "VALUES(?, 'Member One', 'House', 'D', 'CA')", (member,))
    ctx.conn.execute("INSERT INTO congress_reports(report_id, chamber, filer_name, member_key, filed, url, status) "
                     "VALUES(?, 'house', 'X', ?, ?, ?, 'parsed')", (rid, member, filed, f"https://x/{rid}"))
    for i, (ticker, tx_type, tx_date, amount, status) in enumerate(rows, 1):
        ctx.conn.execute("INSERT INTO congress_trades(report_id, row, owner, asset_text, ticker, tx_type, tx_date, "
                         "amount_text, is_etf, is_option, filing_status) VALUES(?,?,'SP',?,?,?,?,?,1,0,?)",
                         (rid, i, ticker, ticker, tx_type, tx_date, amount, status))


LOW, MID, HIGH = "$1,001 - $15,000", "$15,001 - $50,000", "$50,001 - $100,000"


def _c9(ctx):
    _, trades, problems = pipeline._congress_inputs(ctx.conn)
    return sorted((t.report_id, t.row, t.ticker, t.amount_text) for t in trades.get("M1", [])), problems


def test_amended_row_replaces_its_original(ctx):
    _c9_report(ctx, "H1", "2026-01-10", [("SPY", "P", "2026-01-02", LOW, "New"), ("RSP", "P", "2026-01-02", LOW, "New")])
    _c9_report(ctx, "H2", "2026-02-01", [("SPY", "P", "2026-01-02", MID, "Amended")])
    assert _c9(ctx) == ([("H1", 2, "RSP", LOW), ("H2", 1, "SPY", MID)], [])


def test_deleted_row_matches_exact_amount_first_then_a_unique_trade(ctx):
    _c9_report(ctx, "H1", "2026-01-10", [("SPY", "P", "2026-01-02", LOW, "New"), ("SPY", "P", "2026-01-02", MID, "New"),
                                         ("RSP", "S", "2026-01-03", LOW, "New")])
    _c9_report(ctx, "H2", "2026-02-01", [("SPY", "P", "2026-01-02", MID, "Deleted"),     # exact amount
                                         ("RSP", "S", "2026-01-03", HIGH, "Deleted")])   # only RSP sale that day
    trades, problems = _c9(ctx)
    assert trades == [("H1", 1, "SPY", LOW)]
    assert [(k, ref, url) for k, ref, _, url in problems] == [("deleted-trade-fuzzy-match", "H2#2", "https://x/H2")]


def test_ambiguous_amended_and_deleted_rows_leave_trades_and_record_issues(ctx):
    _c9_report(ctx, "H1", "2026-01-10", [("SPY", "P", "2026-01-02", LOW, "New"), ("SPY", "P", "2026-01-02", MID, "New")])
    _c9_report(ctx, "H2", "2026-02-01", [("SPY", "P", "2026-01-02", HIGH, "Amended"),    # which one does it amend?
                                         ("SPY", "P", "2026-01-02", "$250,001 - $500,000", "Deleted"),
                                         ("IAU", "P", "2026-01-05", LOW, "Deleted")])     # nothing to delete
    trades, problems = _c9(ctx)
    assert trades == [("H1", 1, "SPY", LOW), ("H1", 2, "SPY", MID), ("H2", 1, "SPY", HIGH)]
    assert sorted((k, ref, url) for k, ref, _, url in problems) == [
        ("amended-trade-unmatched", "H2#1", "https://x/H2"), ("deleted-trade-unmatched", "H2#2", "https://x/H2"),
        ("deleted-trade-unmatched", "H2#3", "https://x/H2")]


def test_identical_trade_on_two_reports_still_counts_once(ctx):
    _c9_report(ctx, "H1", "2026-01-10", [("SPY", "P", "2026-01-02", LOW, "New")])
    _c9_report(ctx, "H2", "2026-02-01", [("SPY", "P", "2026-01-02", LOW, "New")])
    assert _c9(ctx) == ([("H1", 1, "SPY", LOW)], [])


def _named_trade(ctx, rid, row, ticker, asset_text, tx_date, member="M1"):
    ctx.conn.execute("INSERT OR IGNORE INTO members(member_key, name, chamber, party, state) "
                     "VALUES(?, 'Member One', 'House', 'D', 'CA')", (member,))
    ctx.conn.execute("INSERT OR IGNORE INTO congress_reports(report_id, chamber, filer_name, member_key, filed, url, "
                     "status) VALUES(?, 'house', 'X', ?, '2026-03-01', ?, 'parsed')", (rid, member, f"https://x/{rid}"))
    ctx.conn.execute("INSERT INTO congress_trades(report_id, row, owner, asset_text, ticker, tx_type, tx_date, "
                     "amount_text, is_etf, is_option) VALUES(?,?,'SP',?,?,'P',?,?,1,0)",
                     (rid, row, asset_text, ticker, tx_date, LOW))


def test_trades_under_a_ticker_reused_by_a_later_etf_count_only_if_they_name_an_etf(ctx):
    """E4: FB was Facebook's ticker until 2022 and is a ProShares ETF's since 2025-06-26. Its price history starting
    after a reported trade shows the ticker meant something else then; only trades whose text names an ETF count."""
    ctx.conn.execute("INSERT INTO securities(ticker, name, yahoo_type, figi_type, is_etf, checked_at) "
                     "VALUES('FB', 'PROSHARES S&P DYNAMIC BUFFER', 'ETF', 'ETP', 1, 'x')")
    ctx.conn.executemany("INSERT INTO prices_daily(ticker, day, close) VALUES('FB', ?, 25.0)",
                         [("2025-06-26",), ("2025-06-27",)])
    _named_trade(ctx, "H1", 1, "FB", "Facebook, Inc. - Class A (FB)", "2016-05-02")
    _named_trade(ctx, "H1", 2, "FB", "Meta Platforms, Inc. - Class A (FB)", "2025-11-18")   # old ticker, after the ETF began
    _named_trade(ctx, "H1", 3, "FB", "ProShares S&P Dynamic Buffer ETF (FB)", "2025-12-01")
    _, trades, problems = pipeline._congress_inputs(ctx.conn)
    assert [(t.report_id, t.row) for t in trades["M1"]] == [("H1", 3)]
    assert [(k, ref) for k, ref, _, _ in problems] == [("reused-ticker", "H1#1"), ("reused-ticker", "H1#2")]
    assert problems[0][2] == ("FB is now PROSHARES S&P DYNAMIC BUFFER, whose prices start on 2025-06-26, after "
                              "trades reported under FB; 'Facebook, Inc. - Class A (FB)' does not name an ETF, so it "
                              "is not counted as one (E4)")


def test_member_who_left_office_is_not_ranked_after_leaving(ctx, tmp_path):
    (ctx.raw / "legislators-historical.json").write_text(json.dumps([{
        "id": {"bioguide": "X000001"}, "name": {"first": "Ex", "last": "Member"},
        "terms": [{"type": "rep", "state": "CA", "district": 1, "party": "Democrat", "start": "2019-01-03",
                   "end": "2025-01-03"}]}]))
    _c9_report(ctx, "H1", "2024-03-01", [("SPY", "P", "2024-02-01", LOW, "New")], member="X000001")
    ensure_securities(ctx, {"SPY"})
    managers, famous = _spy_boards(ctx, tmp_path)
    build_boards(ctx, managers, famous)
    b = {r[0]: json.loads(r[1]) for r in ctx.conn.execute("SELECT win, json FROM leaderboards WHERE grp='congress'")}
    assert b["1y"]["ranked"] == b["1y"]["unranked"] == []
    assert [(r["id"], r["reason"]) for r in b["3y"]["unranked"]] == [
        ("X000001", "left office on 2025-01-03; trades after that are not reported")]


def test_build_boards_needs_the_member_terms_once_there_are_congress_trades(ctx, tmp_path):
    """Without them former members would be ranked again (C11); before any trade is stored they are not needed."""
    (ctx.raw / "legislators-current.json").unlink()
    ensure_securities(ctx, {"SPY"})
    managers, famous = _spy_boards(ctx, tmp_path)
    build_boards(ctx, managers, famous)
    _c9_report(ctx, "H1", "2024-03-01", [("SPY", "P", "2024-02-01", LOW, "New")])
    with pytest.raises(FileNotFoundError):
        build_boards(ctx, managers, famous)


def test_c9_issues_are_recorded_by_build_boards(ctx, tmp_path, monkeypatch):
    _c9_report(ctx, "H2", "2026-02-01", [("SPY", "P", "2026-01-02", LOW, "Deleted")])
    ensure_securities(ctx, {"SPY"})
    managers, famous = _spy_boards(ctx, tmp_path)
    build_boards(ctx, managers, famous)
    got = [tuple(r) for r in ctx.conn.execute("SELECT kind, url FROM issues WHERE ref='H2#1' AND resolved=0")]
    assert got == [("deleted-trade-unmatched", "https://x/H2")]


def test_live_rebuild_leaves_data_issues_as_the_data_rebuild_wrote_them(ctx, tmp_path):
    """Leaderboard issues depend on stored data only, so the 5-minute live rebuild does not rewrite them (~200,000
    rows on the real database); the next data rebuild does."""
    _c9_report(ctx, "H2", "2026-02-01", [("SPY", "P", "2026-01-02", LOW, "Deleted")])
    ensure_securities(ctx, {"SPY"})
    managers, famous = _spy_boards(ctx, tmp_path)
    build_boards(ctx, managers, famous)
    ctx.conn.execute("UPDATE issues SET last_seen='before' WHERE ref='H2#1'")
    build_boards(ctx, managers, famous, live=True)
    assert tuple(ctx.conn.execute("SELECT last_seen, resolved FROM issues WHERE ref='H2#1'").fetchone()) == ("before", 0)
    build_boards(ctx, managers, famous)
    assert ctx.conn.execute("SELECT last_seen FROM issues WHERE ref='H2#1'").fetchone()[0] != "before"


def test_parser_upgrade_reparses_stored_reports_to_read_filing_status(ctx, monkeypatch):
    """Reports parsed before filing status was read have every row stored as 'New'; version 3 re-parses them."""
    d = ctx.raw / "house" / "2014"
    d.mkdir(parents=True)
    (d / "20001268.pdf").write_bytes((OLD_HOUSE / "2014-20001268.pdf").read_bytes())
    (ctx.raw / "house" / "2014FD.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><FinancialDisclosure><Member><Prefix>Hon.</Prefix><Last>Grijalva</Last>'
        '<First>Raúl M.</First><Suffix /><FilingType>P</FilingType><StateDst>AZ03</StateDst><Year>2014</Year>'
        '<FilingDate>7/9/2014</FilingDate><DocID>20001268</DocID></Member></FinancialDisclosure>')
    row = _old_row("20001268", "Grijalva", "Raúl M.", "AZ03", date(2014, 7, 9))
    ingest_house_report(ctx, [], row, d / "20001268.pdf", pipeline.house_src.pdf_url(2014, "20001268"))
    ctx.conn.execute("UPDATE congress_trades SET filing_status='New'")          # as the version-2 code stored it
    db.set_meta(ctx.conn, "house-parser-version", "2")

    class Client:
        def __init__(self, raw): pass
        def index(self, y): return []
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    pipeline.sync_house(ctx, [2026])
    assert [r[0] for r in ctx.conn.execute("SELECT filing_status FROM congress_trades")] == ["Deleted", "Deleted"]
    assert db.get_meta(ctx.conn, "house-parser-version") == "5"


def test_put_slash_rows_from_real_reports_are_options(ctx):
    for doc, filed in (("20007014", date(2017, 4, 6)), ("20008680", date(2018, 1, 5))):
        row = _old_row(doc, "Trott", "David A.", "MI11", filed)
        y = filed.year
        assert ingest_house_report(ctx, [], row, OLD_HOUSE / f"{y}-{doc}.pdf", f"https://x/{y}/{doc}.pdf") == "parsed"
    flagged = [(r[0], r[1]) for r in ctx.conn.execute(
        "SELECT report_id, row FROM congress_trades WHERE is_option=1 ORDER BY report_id, row")]
    assert flagged == [("H20007014", 2), ("H20007014", 3), ("H20007014", 4), ("H20007014", 5),
                       ("H20008680", 3), ("H20008680", 4), ("H20008680", 5), ("H20008680", 6), ("H20008680", 7)]
    texts = {r[0] for r in ctx.conn.execute("SELECT asset_text FROM congress_trades WHERE is_option=1")}
    assert "PUT/SPY @ 200 EXP 12/20/2019 - SPDR S&P 500 ETF TRUST" in texts


def test_reprocess_records_issues_and_continues_past_a_failing_report(ctx, monkeypatch):
    _stored_old_rejection(ctx, monkeypatch)
    for doc in ("20000001", "20000002"):                      # one without its PDF, one not in the index
        ctx.conn.execute("INSERT INTO congress_reports(report_id, chamber, filer_name, filed, url, status, reason) "
                         "VALUES(?, 'house', 'X', '2018-02-15', ?, 'rejected', 'missing header field Name')",
                         (f"H{doc}", f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2018/{doc}.pdf"))
    (ctx.raw / "house" / "2018" / "20000002.pdf").write_bytes(b"%PDF")
    ctx.conn.execute("INSERT INTO congress_reports(report_id, chamber, filer_name, filed, url, status, reason) "
                     "VALUES('H20008974', 'house', 'X', '2018-02-15', "
                     "'https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2018/20008974.pdf', 'rejected', "
                     "'missing header field Name')")
    (ctx.raw / "house" / "2018" / "20008974.pdf").write_bytes(b"%PDF")
    xml = ctx.raw / "house" / "2018FD.xml"
    xml.write_text(xml.read_text().replace("</FinancialDisclosure>",
                   "<Member><Prefix>Hon.</Prefix><Last>Langevin</Last><First>James R.</First><Suffix />"
                   "<FilingType>P</FilingType><StateDst>RI02</StateDst><Year>2018</Year>"
                   "<FilingDate>2/16/2018</FilingDate><DocID>20008974</DocID></Member></FinancialDisclosure>"))
    real = pipeline.ingest_house_report

    def ingest(ctx_, terms, row, path, url):
        if row.doc_id == "20008974":
            raise RuntimeError("boom")
        return real(ctx_, terms, row, path, url)
    monkeypatch.setattr(pipeline, "ingest_house_report", ingest)
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    out = pipeline.reprocess_rejected_house(ctx)
    assert out == {"parsed": 1, "missing-pdf": 1, "missing-index-row": 1, "failed": 1}
    got = {(r[0], r[1]) for r in ctx.conn.execute("SELECT kind, ref FROM issues WHERE source='house' AND resolved=0 "
                                                  "AND kind LIKE 'reparse-%'")}
    assert got == {("reparse-missing-pdf", "H20000001"), ("reparse-missing-index-row", "H20000002"),
                   ("reparse-failed", "H20008974")}


def test_parser_upgrade_rereads_reports_rejected_for_unplaced_text(ctx, monkeypatch):
    """Version 4 reads subholding text wrapped onto lines of its own; the reports version 3 rejected for it
    ("unparsed text before a transaction: ...") are read again from their cached PDF. Other rejections stay."""
    from app.sources.errors import ParseRejected
    d = ctx.raw / "house" / "2022"
    d.mkdir(parents=True, exist_ok=True)
    (d / "20021754.pdf").write_bytes((FIX / "house" / "20021754.pdf").read_bytes())
    (ctx.raw / "house" / "2022FD.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><FinancialDisclosure><Member><Prefix>Hon.</Prefix><Last>Manning</Last>'
        '<First>Kathy</First><Suffix /><FilingType>P</FilingType><StateDst>NC06</StateDst><Year>2022</Year>'
        '<FilingDate>10/11/2022</FilingDate><DocID>20021754</DocID></Member></FinancialDisclosure>')
    row = pipeline.house_src.parse_index_xml((ctx.raw / "house" / "2022FD.xml").read_bytes())[0]
    for rid_row, reason in ((row, "unparsed text before a transaction: Kaplan Alaska LLC"),
                            (replace(row, doc_id="20021755"), "found 3 asset codes but parsed 1 transactions")):
        with monkeypatch.context() as m:                     # as version 3 stored them
            m.setattr(pipeline.house_src, "parse_pdf", lambda p, reason=reason: (_ for _ in ()).throw(ParseRejected(reason)))
            assert ingest_house_report(ctx, [], rid_row, d / "20021754.pdf",
                                       pipeline.house_src.pdf_url(2022, rid_row.doc_id)) == "rejected"
    db.set_meta(ctx.conn, "house-parser-version", "3")

    class Client:
        def __init__(self, raw): pass
        def index(self, y): return []
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    pipeline.sync_house(ctx, [2026])
    status = dict(ctx.conn.execute("SELECT report_id, status FROM congress_reports").fetchall())
    assert status == {"H20021754": "parsed", "H20021755": "rejected"}
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_trades WHERE report_id='H20021754'").fetchone()[0] == 17
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE ref='H20021754' AND resolved=0 "
                            "AND kind='rejected'").fetchone()[0] == 0
    assert db.get_meta(ctx.conn, "house-parser-version") == "5"


def test_parser_version_is_not_bumped_when_reprocessing_stops(ctx, monkeypatch):
    class Client:
        def __init__(self, raw): pass
        def index(self, y): return []
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)

    def stop(c):
        raise KeyboardInterrupt
    monkeypatch.setattr(pipeline, "reprocess_parsed_house", stop)
    with pytest.raises(KeyboardInterrupt):
        pipeline.sync_house(ctx, [2026])
    assert db.get_meta(ctx.conn, "house-parser-version") is None


GILL_TX32 = ('<?xml version="1.0" encoding="utf-8"?><FinancialDisclosure><Member><Prefix>{prefix}</Prefix>'
             '<Last>Gill</Last><First>Brandon</First><Suffix /><FilingType>P</FilingType><StateDst>TX32</StateDst>'
             '<Year>2026</Year><FilingDate>9/9/2026</FilingDate><DocID>20035410</DocID></Member></FinancialDisclosure>')


def test_matching_upgrade_files_name_only_reports_under_their_member(ctx, monkeypatch):
    """Version 5 (C8): a report kept under a name-only member because the index gave another district of the
    state (Gill's report listed as TX32) is read again and filed under the member; the name-only member goes."""
    d = ctx.raw / "house" / "2026"
    d.mkdir(parents=True)
    (d / "20035410.pdf").write_bytes((FIX / "house" / "20035410.pdf").read_bytes())
    (ctx.raw / "house" / "2026FD.xml").write_text(GILL_TX32.format(prefix="Hon."))
    row = pipeline.house_src.parse_index_xml((ctx.raw / "house" / "2026FD.xml").read_bytes())[0]
    ingest_house_report(ctx, [], row, d / "20035410.pdf", pipeline.house_src.pdf_url(2026, "20035410"))
    assert ctx.conn.execute("SELECT member_key FROM congress_reports").fetchone()[0] == "H-TX32-Gill"   # as v4 kept it
    db.set_meta(ctx.conn, "house-parser-version", "4")

    class Client:
        def __init__(self, raw): pass
        def index(self, y): return []
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    pipeline.sync_house(ctx, [2026])
    r = ctx.conn.execute("SELECT status, member_key FROM congress_reports").fetchone()
    assert (r["status"], r["member_key"]) == ("parsed", "G000603")
    assert [m[0] for m in ctx.conn.execute("SELECT member_key FROM members")] == ["G000603"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_trades WHERE report_id='H20035410'").fetchone()[0] == 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE resolved=0").fetchone()[0] == 0
    assert db.get_meta(ctx.conn, "house-parser-version") == "5"


def test_matching_upgrade_files_a_late_senate_report_under_its_senator(ctx, monkeypatch):
    """Perdue's report filed four months after he left was kept under a name-only member, so he showed twice. Its
    listing is not stored: the name to match again comes from the name-only key, S-{last}-{first}."""
    (ctx.raw / "legislators-historical.json").write_text(json.dumps([{
        "id": {"bioguide": "P000612"}, "name": {"first": "David", "last": "Perdue", "official_full": "David Perdue"},
        "terms": [{"type": "sen", "state": "GA", "party": "Republican", "start": "2015-01-06", "end": "2021-01-03"}]}]))
    key = "S-Perdue , Jr-David A"                                        # as sync_senate kept it
    ctx.conn.execute("INSERT INTO members(member_key, name, chamber, party, state) "
                     "VALUES(?, 'David A Perdue , Jr', 'Senate', '?', '')", (key,))
    ctx.conn.execute("INSERT INTO congress_reports(report_id, chamber, filer_name, member_key, filed, url, status) "
                     "VALUES('S1', 'senate', 'David A Perdue , Jr', ?, '2021-05-02', 'https://x/S1', 'parsed')", (key,))
    issues.record(ctx.conn, "senate", "member-not-matched", "S1", "David A Perdue , Jr: senate: no candidate")
    db.set_meta(ctx.conn, "house-parser-version", "4")

    class Client:
        def __init__(self, raw): pass
        def index(self, y): return []
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    pipeline.sync_house(ctx, [2026])
    assert ctx.conn.execute("SELECT member_key FROM congress_reports").fetchone()[0] == "P000612"
    assert [tuple(m) for m in ctx.conn.execute("SELECT member_key, party, state FROM members")] == [("P000612", "R", "GA")]
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE resolved=0").fetchone()[0] == 0


def test_matching_upgrade_reads_reports_of_filers_that_matched_no_member(ctx, monkeypatch):
    """A filer who matched no member was stored without trades and its PDF was never downloaded. Version 5 drops
    such reports, and the same run reads every year's index again: a report whose filer now matches is read."""
    row = pipeline.house_src.parse_index_xml(GILL_TX32.format(prefix="Mr.").encode())[0]   # not "Hon.": no fallback
    url = pipeline.house_src.pdf_url(2026, "20035410")
    assert ingest_house_report(ctx, [], row, None, url) == "unmatched"                     # as version 4 kept it
    db.set_meta(ctx.conn, "house-parser-version", "4")
    years, pdfs = [], []

    class Client:
        def __init__(self, raw): pass

        def index(self, y):
            years.append(y)
            return [row] if y == 2026 else []

        def pdf(self, y, doc_id):
            pdfs.append((y, doc_id))
            return FIX / "house" / "20035410.pdf"
    monkeypatch.setattr(pipeline.house_src, "HouseClient", Client)
    monkeypatch.setattr(pipeline, "resolve_congress_tickers", lambda c: {})
    pipeline.sync_house(ctx)
    assert years == list(range(2012, 2027)) and pdfs == [(2026, "20035410")]
    r = ctx.conn.execute("SELECT status, member_key FROM congress_reports").fetchone()
    assert (r["status"], r["member_key"]) == ("parsed", "G000603")
    assert db.get_meta(ctx.conn, "house-catch-up") is None


# ---------- run-1 fixes ----------

def test_sync_13f_datasets_skips_unrecognized_name_with_issue(ctx, monkeypatch):
    base = "https://www.sec.gov/files/x/"
    page = f'<a href="{base}weird-name_form13f.zip">w</a><a href="{base}2013q2_form13f.zip">old</a>'
    monkeypatch.setattr(Ctx, "sec", lambda self, url: page.encode())
    downloads = []
    monkeypatch.setattr(pipeline, "_download", lambda url, path, sec: downloads.append(url) or path)
    out = pipeline.sync_13f_datasets(ctx)
    assert out == {"datasets": 0}                      # 2013q2 is older than the 5-year window, weird name skipped
    rows = ctx.conn.execute("SELECT source, kind, ref FROM issues WHERE kind='dataset-name-unrecognized'").fetchall()
    assert [tuple(r) for r in rows] == [("sec13f", "dataset-name-unrecognized", base + "weird-name_form13f.zip")]
    assert downloads == []


def _senate_client(listings, html):
    class Client:
        def __init__(self, visible=False): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def search(self, since): return listings
        def page(self, url): return html(url) if callable(html) else html
    return Client


def _lst(uuid, first="Jane", last="Smith", filed=date(2014, 3, 5)):
    return SenateListing(first, last, f"{last}, {first} (Senator)", f"https://x/ptr/{uuid}/", "ptr",
                         "Periodic Transaction Report for 03/01/2014", filed, date(2014, 3, 1), 0)


def test_senate_page_with_repeated_hash_numbers_is_stored(ctx, monkeypatch):
    ctx = replace(ctx, cfg=replace(ctx.cfg, senate_enabled=True))
    html = (FIX / "senate" / "ptr_26ae70ce-c44b-4186-b6f3-89b7b9a00b0a.html").read_text()
    monkeypatch.setattr(pipeline.senate_src, "SenateClient", _senate_client([_lst("abc")], html))
    assert pipeline.sync_senate(ctx).get("parsed") == 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_trades WHERE report_id='Sabc'").fetchone()[0] == 7


def test_senate_access_denied_waits_before_the_browser_is_opened_again(ctx, monkeypatch):
    """A refused site is not asked again every 15 minutes (the scheduler's retry): each try opens a browser window
    and may prolong the block. The job keeps failing, with the reason, until the wait is over."""
    from datetime import timedelta
    clock = {"now": NOW}
    ctx = replace(ctx, cfg=replace(ctx.cfg, senate_enabled=True), now=lambda: clock["now"])
    opened = []

    class Denied:
        def __init__(self, visible=False): pass
        def __enter__(self):
            opened.append(clock["now"])
            raise pipeline.senate_src.AccessDenied("efdsearch.senate.gov refused the browser (HTTP 403 Access Denied)")
        def __exit__(self, *a): return False

    monkeypatch.setattr(pipeline.senate_src, "SenateClient", Denied)
    with pytest.raises(pipeline.senate_src.AccessDenied, match="HTTP 403"):
        pipeline.sync_senate(ctx)
    clock["now"] = NOW + timedelta(minutes=15)
    with pytest.raises(pipeline.senate_src.AccessDenied, match="not tried again before"):
        pipeline.sync_senate(ctx)
    assert opened == [NOW]
    clock["now"] = NOW + pipeline.SENATE_DENIED_WAIT
    monkeypatch.setattr(pipeline.senate_src, "SenateClient", _senate_client([], ""))
    assert pipeline.sync_senate(ctx) == {}


def test_senate_duplicate_listing_urls_are_deduped(ctx, monkeypatch):
    ctx = replace(ctx, cfg=replace(ctx.cfg, senate_enabled=True))
    html = (FIX / "senate" / "ptr_0541be4f-4f96-4d84-8d6f-d349f218bb2a.html").read_text()
    monkeypatch.setattr(pipeline.senate_src, "SenateClient", _senate_client([_lst("abc"), _lst("abc")], html))
    assert pipeline.sync_senate(ctx).get("parsed") == 1


def test_senate_integrity_error_in_one_listing_does_not_abort_job(ctx, monkeypatch):
    import sqlite3
    ctx = replace(ctx, cfg=replace(ctx.cfg, senate_enabled=True))
    html = (FIX / "senate" / "ptr_0541be4f-4f96-4d84-8d6f-d349f218bb2a.html").read_text()
    real = pipeline.senate_src.parse_ptr_html
    def parse(page):
        trades = real(page)
        return trades + trades[:1] if parse.n == 0 and not setattr(parse, "n", 1) else trades
    parse.n = 0
    monkeypatch.setattr(pipeline.senate_src, "parse_ptr_html", parse)
    monkeypatch.setattr(pipeline.senate_src, "SenateClient", _senate_client([_lst("bad"), _lst("good")], html))
    out = pipeline.sync_senate(ctx)
    assert out.get("parsed") == 1 and out.get("integrity-failed") == 1
    assert ctx.conn.execute("SELECT COUNT(*) FROM congress_reports WHERE report_id='Sgood'").fetchone()[0] == 1
    assert [r[0] for r in ctx.conn.execute("SELECT kind FROM issues WHERE ref='Sbad'")] == ["senate-report-error"]


def _trade(ctx, rid, row, text, ticker=None):
    ctx.conn.execute("INSERT OR IGNORE INTO congress_reports(report_id, chamber, status, url) VALUES(?, 'senate', 'parsed', 'u')", (rid,))
    ctx.conn.execute("INSERT INTO congress_trades(report_id, row, asset_text, ticker, is_option) VALUES(?,?,?,?,0)",
                     (rid, row, text, ticker))
    ctx.conn.commit()


def test_possible_etf_issue_only_when_no_candidate_is_a_known_non_etp(ctx):
    _trade(ctx, "S1", 1, "Schwab ETF Fake Corp (XYZ)")        # looks like an ETF, but OpenFIGI says Common Stock
    _trade(ctx, "S1", 2, "Obscure Growth ETF")                # no candidate at all
    _trade(ctx, "S1", 3, "Accenture plc Class A Ordinary Shares (ACN)")   # does not look like an ETF
    resolve_congress_tickers(ctx)
    refs = [r[0] for r in ctx.conn.execute("SELECT ref FROM issues WHERE kind='unmatched-possible-etf'")]
    assert refs == ["S1#2"]


def test_reevaluate_possible_etf_issues_resolves_only_false_positives(ctx):
    _trade(ctx, "S1", 1, "Accenture plc Class A Ordinary Shares (ACN)", "ACN")
    _trade(ctx, "S1", 2, "ISHARES IAI")
    ctx.conn.execute("INSERT INTO securities(ticker, name, figi_type, is_etf, checked_at) VALUES('ACN','Accenture','Common Stock',0,'2026-10-01')")
    for ref in ("S1#1", "S1#2"):
        issues.record(ctx.conn, "congress", "unmatched-possible-etf", ref, "old", "u")
    ctx.conn.commit()
    out = pipeline.reevaluate_possible_etf_issues(ctx.conn)
    assert out == {"checked": 2, "resolved": 1}
    open_refs = [r[0] for r in ctx.conn.execute("SELECT ref FROM issues WHERE kind='unmatched-possible-etf' AND resolved=0")]
    assert open_refs == ["S1#2"]
    assert db.get_meta(ctx.conn, "etf-issue-rule-version") == "2"
    assert pipeline.reevaluate_possible_etf_issues(ctx.conn) == {"skipped": "already done"}


def test_store_chart_records_issue_for_dropped_bad_closes(ctx):
    chart = replace(FakeYahoo().history("RSP"), dropped_bad_closes=2)
    _store_chart(ctx, chart)
    r = ctx.conn.execute("SELECT message FROM issues WHERE source='prices' AND kind='bad-close-dropped' AND ref='RSP' "
                         "AND resolved=0").fetchall()
    assert [x[0] for x in r] == ["RSP: 2 day(s) with a zero/invalid close were ignored"]
    _store_chart(ctx, replace(chart, dropped_bad_closes=0))        # clean re-fetch clears it
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='bad-close-dropped' AND resolved=0").fetchone()[0] == 0


# ---------- 13F book: the value of a filing's share rows ----------

def test_ingested_13f_filing_stores_its_book(ctx):
    """"% of book" divides by the value of the filing's share rows (no options, no principal amounts): the rows ETF
    positions come from, so both sides are in the filer's own units. The summary page's total (1,240,161,751 here)
    also counts the excluded rows and is sometimes entered in other units."""
    ingest_dataset(ctx, DataSet(FIX / "sec13f" / "diameter_2026q2_restatement"), date(2021, 9, 30))
    assert [tuple(r) for r in ctx.conn.execute("SELECT accession, book_usd FROM f13_books")] == [
        ("0001172661-26-003822", 759862394.0)]


def _cached_dataset(ctx, name):
    import zipfile
    src = FIX / "sec13f" / "diameter_2026q2_restatement"
    d = ctx.raw / "sec13f"
    d.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(d / name, "w") as z:
        for p in src.iterdir():
            z.write(p, p.name)
    return src


def test_book_backfill_recomputes_from_cached_data_sets(ctx):
    url = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip"
    src = _cached_dataset(ctx, url.rsplit("/", 1)[-1])
    ingest_dataset(ctx, DataSet(src), date(2021, 9, 30))
    with db.transaction(ctx.conn):
        db.set_meta(ctx.conn, "13f-ds:" + url, "done")
        ctx.conn.execute("DELETE FROM f13_books")                             # as stored before books were kept
        ctx.conn.execute("INSERT INTO f13_filings VALUES('E-1','5','Edgar Filer','2026-06-30','2026-08-01',"
                         "'13F-HR',NULL)")
        ctx.conn.execute("INSERT INTO f13_filing_totals VALUES('E-1', 3, 500.0)")    # EDGAR: sum of its share rows
    assert pipeline.backfill_13f_books(ctx) == {"from_data_sets": 1, "from_edgar_totals": 1}
    assert {r[0]: r[1] for r in ctx.conn.execute("SELECT accession, book_usd FROM f13_books")} == {
        "0001172661-26-003822": 759862394.0, "E-1": 500.0}
    assert pipeline.backfill_13f_books(ctx) == {}                            # done once


def test_book_backfill_leaves_unknown_what_it_cannot_recompute(ctx):
    with db.transaction(ctx.conn):
        db.set_meta(ctx.conn, "13f-ds:https://x/2023q1_form13f.zip", "done")    # its file is not cached any more
        ctx.conn.execute("INSERT INTO f13_filings VALUES('E-1','5','F','2026-06-30','2026-08-01','13F-HR',NULL)")
        ctx.conn.execute("INSERT INTO f13_filing_totals VALUES('E-1', 3, 500.0)")
    assert pipeline.backfill_13f_books(ctx) == {"from_data_sets": 0, "from_edgar_totals": 0}
    assert ctx.conn.execute("SELECT COUNT(*) FROM f13_books").fetchone()[0] == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM issues WHERE kind='book-unknown' AND resolved=0").fetchone()[0] == 1


def _13f(conn, acc, cik, filed, form="13F-HR", amend=None, etf=(), book=None, period="2026-03-31"):
    conn.execute("INSERT INTO f13_filings VALUES(?,?,?,?,?,?,?)", (acc, cik, "F", period, filed, form, amend))
    conn.executemany("INSERT INTO f13_holdings VALUES(?,?,?,?,?,?)",
                     [(acc, f"C{i}", 1.0, v, "X", "ETF") for i, v in enumerate(etf)])
    if book is not None:
        conn.execute("INSERT INTO f13_books VALUES(?,?)", (acc, book))


def test_13f_period_stats_follow_the_amendment_rules(ctx):
    c = ctx.conn
    with db.transaction(c):
        _13f(c, "a1", "1", "2026-05-01", etf=(10, 30), book=200)                                   # 40 of 200
        _13f(c, "b1", "2", "2026-05-01", etf=(50,), book=100)
        _13f(c, "b2", "2", "2026-05-20", "13F-HR/A", "RESTATEMENT", etf=(20,), book=400)           # replaces: 20/400
        _13f(c, "c1", "3", "2026-05-01", etf=(10,), book=100)
        _13f(c, "c2", "3", "2026-05-20", "13F-HR/A", "NEW HOLDINGS", etf=(5,), book=50)            # adds: 15/150
        _13f(c, "c3", "3", "2026-05-25", "13F-HR/A", "", etf=(999,), book=1)                       # unknown: ignored
        _13f(c, "d1", "4", "2026-05-01", etf=(10,))                                                  # book unknown
        _13f(c, "e1", "5", "2026-05-01", book=0.0)                                                   # nothing reported
    pipeline.refresh_13f_stats(c, NOW)
    got = {(r[0], r[1]): (r[2], r[3]) for r in c.execute("SELECT cik, period, etf_usd, book_usd FROM f13_period_stats")}
    q = "2026-03-31"
    assert got == {("1", q): (40, 200), ("2", q): (20, 400), ("3", q): (15, 150), ("4", q): (10, None),
                   ("5", q): (0, 0.0)}
    d = date(2026, 3, 31)
    assert pipeline.etf_shares(c) == {"1": {d: 0.2}, "2": {d: 0.05}, "3": {d: 0.1}, "4": {d: None}, "5": {d: None}}
    assert db.get_meta(c, "13f-stats-at") == NOW.isoformat()
