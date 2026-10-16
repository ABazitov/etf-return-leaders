"""Jobs that move data from the sources into the database and rebuild the leaderboards (spec §10-§12)."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
from array import array
from contextlib import ExitStack
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from xml.etree.ElementTree import ParseError

from app import db, issues
from app.config import Config, sec_user_agent
from app.congress_sim import TX_KIND, parse_amount
from app.curated import (
    DIVIDEND_CORRECTIONS_PATH, FAMOUS_PATH, MANAGERS_PATH, is_stale, load_dividend_corrections, load_famous,
    load_managers,
)
from app.etf_check import (
    OpenFigiClient, etf_name_index, is_etf, looks_like_etf, match_by_name, parse_nasdaq_directory, ticker_candidates,
)
from app.http import HttpError, fetch
from app.leaderboards import (
    WINDOWS, CTrade, DenseSeries, FilerHistory, MemberInfo, Position, PriceBook, assemble, benchmark_series,
    cannot_be_listed, congress_board, investors_board, last_quarter_end_on_or_before, make_window, managers_board,
)
from app.members import Term, load_terms, match_house, match_senate, party_letter, service_periods
from app.prices import (
    NY, YahooProvider, apply_dividend_corrections, completed_rows, x1_check, x5_flags, x6_irregular_dividends,
)
from app.returns import build_tri, simple_return
from app.sources import house as house_src
from app.sources import sec13f
from app.sources import senate as senate_src
from app.sources.errors import ParseRejected

log = logging.getLogger("etf.pipeline")
FIRST_YEAR = 2012
TRI_FROM = date(2012, 1, 1)
UA = "ETF-Return-Leaders/1.0"
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-{}.json"
DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
CURRENT_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb="
                "&owner=include&start=0&count=100&output=atom")
DAILY_IDX = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
X5_JUMP = 0.5          # X5: a move this large with no split recorded is suspicious


@dataclass
class Ctx:
    cfg: Config
    conn: object
    raw: Path
    now: Callable[[], datetime] = field(default=lambda: datetime.now(NY))
    yahoo: object = field(default_factory=YahooProvider)
    figi: object = None
    progress: dict = field(default_factory=dict)

    def sec(self, url: str) -> bytes:
        return fetch(url, ua=sec_user_agent(self.cfg), min_interval=0.2)

    def fork(self) -> Ctx:
        """The same context with its own connection to the same database file, for another thread."""
        path = self.conn.execute("PRAGMA database_list").fetchone()["file"]
        if not path:
            raise RuntimeError("an in-memory database cannot be opened from another thread")
        return replace(self, conn=db.connect(path))


def make_ctx(cfg: Config, conn) -> Ctx:
    raw = cfg.data_dir / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    return Ctx(cfg, conn, raw, figi=OpenFigiClient(cfg.openfigi_api_key))


def _now_iso(ctx: Ctx) -> str:
    return ctx.now().isoformat(timespec="seconds")


def _download(url: str, dest: Path, getter: Callable[[str], bytes]) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(getter(url))
        tmp.rename(dest)
    return dest


# ---------- reference data ----------

def refresh_reference(ctx: Ctx) -> dict:
    (ctx.raw / "nasdaqtraded.txt").write_bytes(fetch(NASDAQ_URL, ua=UA))
    for kind in ("current", "historical"):
        (ctx.raw / f"legislators-{kind}.json").write_bytes(fetch(LEGISLATORS_URL.format(kind), ua=UA))
    return {"files": 3}


def _terms(ctx: Ctx) -> list[Term]:
    people = []
    for kind in ("current", "historical"):
        people += json.loads((ctx.raw / f"legislators-{kind}.json").read_text())
    return load_terms(people)


def _etf_names(ctx: Ctx) -> dict[str, list[str]]:
    return etf_name_index(parse_nasdaq_directory((ctx.raw / "nasdaqtraded.txt").read_text(errors="replace")))


# ---------- securities and prices ----------

def _curation_today(ctx: Ctx) -> date:
    """The date a curated entry may at most be verified on: the context's date, or the computer's own date if that
    is later. In production both are today (they differ only around midnight, when New York is still a day behind
    the computer's time zone: an entry verified "today" there must not be rejected as from the future). In tests
    with a fixed past clock, the real date keeps the shipped curated files, verified on their real date, loadable."""
    return max(ctx.now().date(), date.today())


def _corrections_for(ctx: Ctx, ticker: str, path: Path | None = None) -> list:
    path = path or DIVIDEND_CORRECTIONS_PATH
    return [c for c in load_dividend_corrections(path, _curation_today(ctx)) if c.ticker == ticker]


def _store_chart(ctx: Ctx, chart) -> bool:
    raw_rows = completed_rows(chart.rows, ctx.now())
    ok, worst, when = x1_check(raw_rows)         # X1 compares with Yahoo's adjclose, so it sees Yahoo's own dividends
    corrections = _corrections_for(ctx, chart.ticker)
    rows, unmatched = apply_dividend_corrections(raw_rows, corrections)
    applied = [c for c in corrections if c not in unmatched]
    jumps_x6 = x6_irregular_dividends(rows)
    jumps = x5_flags(rows)
    t, conn = chart.ticker, ctx.conn
    today = ctx.now().date()
    # Yahoo adjusts the whole history for a split as soon as it is effective. If this chart already shows a split
    # today, what we store is in post-split units, and the live value must not apply the split ratio again.
    split_in_chart = any(r.day == today and r.split != 1.0 for r in chart.rows) and \
        not any(r.day == today for r in raw_rows)
    with db.transaction(conn):
        if split_in_chart:
            key = f"split-applied:{today.isoformat()}"
            done = set(json.loads(db.get_meta(conn, key, "[]")))
            db.set_meta(conn, key, json.dumps(sorted(done | {t})))
        conn.execute("DELETE FROM prices_daily WHERE ticker=?", (t,))
        conn.executemany("INSERT INTO prices_daily(ticker, day, close, dividend, split, adjclose) VALUES(?,?,?,?,?,?)",
                         [(t, r.day.isoformat(), r.close, r.dividend, r.split, r.adjclose) for r in rows])
        sec = conn.execute("SELECT figi_type, yahoo_type, name FROM securities WHERE ticker=?", (t,)).fetchone()
        etf_now = None
        if sec and sec["figi_type"] == "ETP" and sec["yahoo_type"] is None:     # earlier Yahoo failure: apply E1 now
            etf_now = is_etf("ETP", chart.instrument_type, sec["name"] or "")
            conn.execute("UPDATE securities SET yahoo_type=?, is_etf=? WHERE ticker=?",
                         (chart.instrument_type, int(etf_now), t))
            ok = ok and etf_now
            if not etf_now:
                conn.execute("UPDATE congress_trades SET is_etf=0 WHERE ticker=?", (t,))
                conn.execute("DELETE FROM f13_holdings WHERE cusip IN (SELECT cusip FROM cusip_map WHERE ticker=?)", (t,))
        conn.execute("UPDATE securities SET price_ok=? WHERE ticker=?", (int(ok), t))
        issues.clear_ref(conn, "prices", t)
        if chart.dropped_bad_closes > 0:
            issues.record(conn, "prices", "bad-close-dropped", t,
                          f"{t}: {chart.dropped_bad_closes} day(s) with a zero/invalid close were ignored")
        if etf_now is False and "ETN" not in (sec["name"] or "").upper():
            issues.clear_ref(conn, "etf-check", t)
            issues.record(conn, "etf-check", "x3-identity", t,
                          f"{t}: OpenFIGI says exchange-traded product but Yahoo says {chart.instrument_type}; not counted as an ETF")
        if not ok and etf_now is not False:
            detail = (f"differs from Yahoo's adjusted close by {worst * 100:.2f}% (worst at {when})"
                      if when is not None else "no adjusted prices to check")
            issues.record(conn, "prices", "x1-dividend-check", t,
                          f"{t}: reinvested-dividend index: {detail}; treated as unpriceable")
        if unmatched:
            issues.record(conn, "prices", "dividend-correction-unmatched", t,
                          f"{t}: dividend correction for " + ", ".join(
                              f"{c.ex_date.isoformat()} ({c.source_url})" for c in unmatched)
                          + " matches no trading day in Yahoo's history; check the ex-date in dividend_corrections.json")
        if applied:
            issues.record(conn, "prices", "dividend-corrected", t,
                          f"{t}: dividend corrected: " + "; ".join(
                              f"{c.ex_date.isoformat()} {c.action}" + (f" to {c.amount}" if c.action == "set" else "")
                              + f" ({c.source_url})" for c in applied))
        if jumps_x6:
            by_day = {r.day: r.dividend for r in rows}
            issues.record(conn, "prices", "x6-irregular-dividend", t,
                          f"{t}: irregular dividends on " + ", ".join(f"{d.isoformat()} ({by_day[d]})" for d in jumps_x6)
                          + "; verify against the fund's own reports; add a dividend correction if wrong")
        if jumps:
            issues.record(conn, "prices", "x5-big-move", t,
                          f"{t}: close moved more than 50% with no split on {', '.join(d.isoformat() for d in jumps)}")
    return ok


def ensure_securities(ctx: Ctx, tickers: set[str], max_age_days: int = 30) -> dict[str, bool]:
    conn, out, todo = ctx.conn, {}, []
    cutoff = (ctx.now() - timedelta(days=max_age_days)).isoformat()
    for t in sorted(tickers):
        r = conn.execute("SELECT is_etf, checked_at FROM securities WHERE ticker=?", (t,)).fetchone()
        if r and r["checked_at"] >= cutoff:
            out[t] = bool(r["is_etf"])
        else:
            todo.append(t)
    hits = ctx.figi.map_tickers(todo) if todo else []
    for t, hit in zip(todo, hits):
        figi_type, name = (hit.security_type, hit.name) if hit else (None, "")
        yahoo_type, chart = None, None
        if figi_type == "ETP":
            try:
                chart = ctx.yahoo.history(t)
                yahoo_type = chart.instrument_type
            except (HttpError, KeyError, TypeError, ValueError) as e:
                with db.transaction(conn):
                    issues.record(conn, "prices", "yahoo-missing", t, f"{t}: Yahoo has no price data ({e!r})")
        etf = is_etf(figi_type, yahoo_type, name)
        unpriced = False
        if figi_type == "ETP" and chart is None and not re.search(r"\bETN\b|exchange traded notes?", name, re.I):
            etf, unpriced = True, True        # Yahoo failed: keep the ETF, as unpriceable (R11); recheck in 30 days
        with db.transaction(conn):
            conn.execute("""INSERT INTO securities(ticker, name, yahoo_type, figi_type, is_etf, price_ok, evidence, checked_at)
                            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET name=excluded.name,
                            yahoo_type=excluded.yahoo_type, figi_type=excluded.figi_type, is_etf=excluded.is_etf,
                            price_ok=CASE WHEN excluded.price_ok = 0 THEN 0 ELSE price_ok END,
                            evidence=excluded.evidence, checked_at=excluded.checked_at""",
                         (t, name, yahoo_type, figi_type, int(etf), 0 if unpriced else 1,
                          json.dumps({"openfigi": figi_type, "yahoo": yahoo_type}), _now_iso(ctx)))
            issues.clear_ref(conn, "etf-check", t)
            if figi_type == "ETP" and yahoo_type and not etf and "ETN" not in name.upper():
                issues.record(conn, "etf-check", "x3-identity", t,
                              f"{t}: OpenFIGI says exchange-traded product but Yahoo says {yahoo_type}; not counted as an ETF")
        if etf and chart is not None:
            _store_chart(ctx, chart)
        out[t] = etf
    return out


def tracked_tickers(conn, managers_path: Path = MANAGERS_PATH) -> set[str]:
    out = {"SPY"}
    out |= {r[0] for r in conn.execute("""SELECT m.ticker FROM cusip_map m JOIN securities s ON s.ticker = m.ticker
                                          WHERE s.is_etf = 1 AND EXISTS (SELECT 1 FROM f13_holdings h
                                                                         WHERE h.cusip = m.cusip)""")}
    out |= {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM congress_trades WHERE is_etf = 1")}
    out |= {r["ticker"] for r in json.loads(Path(managers_path).read_text())}
    return out


def sync_prices(ctx: Ctx, full: bool = False) -> dict:
    conn, n = ctx.conn, 0
    tickers = sorted(tracked_tickers(conn))
    for t in tickers:
        try:
            chart = ctx.yahoo.history(t, recent=not full)
        except (HttpError, KeyError, TypeError, ValueError) as e:
            with db.transaction(conn):
                issues.record(conn, "prices", "yahoo-missing", t, f"{t}: Yahoo request failed ({e!r})")
            continue
        unresolved = conn.execute("SELECT 1 FROM securities WHERE ticker=? AND figi_type='ETP' AND yahoo_type IS NULL",
                                  (t,)).fetchone()
        if full or unresolved or any(r.split != 1.0 or r.dividend for r in chart.rows):
            try:
                _store_chart(ctx, ctx.yahoo.history(t) if not full else chart)   # splits/dividends: re-fetch everything
            except (HttpError, KeyError, TypeError, ValueError) as e:
                with db.transaction(conn):
                    issues.record(conn, "prices", "yahoo-missing", t, f"{t}: Yahoo request failed ({e!r})")
                continue
        else:
            rows, _ = apply_dividend_corrections(completed_rows(chart.rows, ctx.now()), _corrections_for(ctx, t))
            with db.transaction(conn):
                conn.execute("UPDATE issues SET resolved=1 WHERE source='prices' AND kind='yahoo-missing' AND ref=?", (t,))
                conn.executemany("""INSERT INTO prices_daily(ticker, day, close, dividend, split, adjclose) VALUES(?,?,?,?,?,?)
                                    ON CONFLICT(ticker, day) DO UPDATE SET close=excluded.close, adjclose=excluded.adjclose""",
                                 [(t, r.day.isoformat(), r.close, r.dividend, r.split, r.adjclose) for r in rows])
        n += 1
    if tickers and n == 0:              # Yahoo down or blocking us: show the source as failing, not as fresh
        raise RuntimeError("all Yahoo requests failed")
    return {"tickers": n}


def sync_dividends_today(ctx: Ctx) -> dict:
    """R3: dividends going ex today (and splits effective today) are only visible in the in-progress session's data.
    Reading every tracked ticker takes most of an hour, so what it finds is stored in one transaction at the end: a
    write per failed ticker would wait on each board rebuild in progress."""
    today, found, splits, failed = ctx.now().date(), {}, {}, []
    tickers, n_ok = sorted(tracked_tickers(ctx.conn)), 0
    for t in tickers:
        try:
            chart = ctx.yahoo.history(t, recent=True)
        except (HttpError, KeyError, TypeError, ValueError) as e:
            failed.append((t, f"{t}: Yahoo request failed ({e!r})"))
            continue
        n_ok += 1
        for r in chart.rows:
            if r.day == today and r.dividend:
                found[t] = r.dividend
            if r.day == today and r.split != 1.0:
                splits[t] = r.split
    learned = not (tickers and n_ok == 0)   # keep yesterday's markers out of it if nothing was learned today
    with db.transaction(ctx.conn):
        for t, message in failed:
            issues.record(ctx.conn, "prices", "yahoo-missing", t, message)
        if learned:
            db.set_meta(ctx.conn, f"div-today:{today.isoformat()}", json.dumps(found))
            db.set_meta(ctx.conn, f"split-today:{today.isoformat()}", json.dumps(splits))
    if not learned:
        raise RuntimeError("all Yahoo requests failed")
    return {"ex_dividend_today": len(found), **({"split_today": len(splits)} if splits else {})}


def sync_live(ctx: Ctx) -> dict:
    now = ctx.now()
    prices = ctx.yahoo.latest(sorted(tracked_tickers(ctx.conn)))
    with db.transaction(ctx.conn):
        ctx.conn.executemany("INSERT INTO prices_live(ticker, price, as_of) VALUES(?,?,?) ON CONFLICT(ticker) DO UPDATE "
                             "SET price=excluded.price, as_of=excluded.as_of",
                             [(t, p, when.isoformat()) for t, (p, when) in prices.items()])
        spy = prices.get("SPY")
        is_open = (now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)
                   and spy is not None and spy[1].date() == now.date())
        db.set_meta(ctx.conn, "market", json.dumps({"open": is_open, "as_of": spy[1].isoformat() if spy else None}))
        if now.weekday() < 5 and (now.hour, now.minute) >= (9, 45) and (spy is None or spy[1].date() != now.date()):
            db.set_meta(ctx.conn, f"holiday:{now.date().isoformat()}", "1")
    return {"prices": len(prices)}


def load_book(ctx: Ctx, tickers: set[str]) -> PriceBook:
    conn, now = ctx.conn, ctx.now()
    today = now.date()
    days = [date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT day FROM prices_daily WHERE ticker='SPY' AND day >= ? ORDER BY day", (TRI_FROM.isoformat(),))]
    live = {r["ticker"]: (r["price"], datetime.fromisoformat(r["as_of"]))
            for r in conn.execute("SELECT ticker, price, as_of FROM prices_live")}
    live_day = today if ("SPY" in live and live["SPY"][1].date() == today and days and days[-1] < today) else None
    if live_day:
        days.append(live_day)
    idx = {d: i for i, d in enumerate(days)}
    session = days[-2] if live_day and len(days) > 1 else None      # L: the last completed session
    div_today = json.loads(db.get_meta(conn, f"div-today:{today.isoformat()}", "{}"))
    split_today = json.loads(db.get_meta(conn, f"split-today:{today.isoformat()}", "{}"))
    split_applied = set(json.loads(db.get_meta(conn, f"split-applied:{today.isoformat()}", "[]")))
    tri, close = {}, {}
    splits: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for t, day, ratio in conn.execute("SELECT ticker, day, split FROM prices_daily WHERE split != 1.0 ORDER BY ticker, day"):
        if t in tickers:
            splits[t].append((date.fromisoformat(day), ratio))
    pos = {d.isoformat(): i for i, d in enumerate(days)}      # stored days are ISO strings: no parsing per row
    empty = array("d", [math.nan]) * len(days)
    cur = conn.cursor()
    cur.row_factory = None
    for t in tickers:
        rows = cur.execute("SELECT day, close, dividend FROM prices_daily WHERE ticker=? ORDER BY day", (t,)).fetchall()
        if not rows:
            continue
        series = build_tri(rows)                                 # keyed by the ISO day string
        tv, cv = array("d", empty), array("d", empty)
        for day, c, _ in rows:
            i = pos.get(day)
            if i is not None:
                tv[i], cv[i] = series[day], c
        last, c_last = rows[-1][0], rows[-1][1]                # L's key in `series`, and c(L)
        if live_day and date.fromisoformat(last) == session:
            # R4: E is the live price if newer, else the last close. Carry L forward unless a usable live quote exists.
            tv[idx[live_day]], cv[idx[live_day]] = series[last], c_last
            if t in live and live[t][1].date() == live_day:
                price, ratio = live[t][0], split_today.get(t)
                if ratio and (t in split_applied or (abs(price * ratio / c_last - 1) > X5_JUMP
                                                     and abs(price / c_last - 1) <= X5_JUMP)):
                    # History was re-fetched after the split took effect (marked by _store_chart, or evidently so:
                    # only the unadjusted price is in line with c(L)), so it is already in post-split units. Its
                    # split row is stored only once today's session completes, so the book records the split here.
                    splits[t].append((live_day, ratio))
                    ratio = None
                if ratio:
                    # A split goes effective today. History (and c(L)) is still in pre-split units until the 16:30
                    # re-fetch, while the live price p and Yahoo's dividend D(today) are per post-split share. One
                    # old share became `ratio` new shares (Yahoo's numerator/denominator: 3 for a 3-for-1 split,
                    # 0.1 for a 1-for-10 reverse split), so an old share is now worth (p + D) * ratio. For 3:1 and
                    # p = c(L)/3 that is c(L): no artificial drop; for 1:10 and p = 10 c(L) no artificial jump.
                    tv[idx[live_day]] = series[last] * (price + div_today.get(t, 0.0)) * ratio / c_last
                    cv[idx[live_day]] = price * ratio          # same (pre-split) units as the rest of the series
                elif abs(price / c_last - 1) > X5_JUMP:
                    # Looks like a split we have no ratio for yet (the 09:45 job has not seen it): using p would
                    # show an artificial move, so keep the carried-forward last close until the ratio is known.
                    log.warning("%s: live price %s vs last close %s with no split ratio known; not used", t, price, c_last)
                else:
                    tv[idx[live_day]] = series[last] * (price + div_today.get(t, 0.0)) / c_last     # R3
                    cv[idx[live_day]] = price
        tri[t], close[t] = DenseSeries(idx, tv), DenseSeries(idx, cv)
    bad = {r[0] for r in conn.execute("SELECT ticker FROM securities WHERE price_ok = 0")}
    return PriceBook(tri, close, days, bad, dict(splits))


# ---------- House ----------

def _upsert_member(conn, t: Term) -> None:
    conn.execute("""INSERT INTO members(member_key, name, chamber, party, state, district, bioguide) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(member_key) DO UPDATE SET name=excluded.name, chamber=excluded.chamber,
                    party=excluded.party, state=excluded.state, district=excluded.district""",
                 (t.bioguide, t.full_name, "House" if t.chamber == "rep" else "Senate", party_letter(t.party),
                  t.state, t.district, t.bioguide))


def _fallback_member(conn, key: str, name: str, chamber: str, state: str) -> None:
    """C8: a filer who is clearly a member but has no unique match is shown by name and state only."""
    conn.execute("INSERT INTO members(member_key, name, chamber, party, state, district, bioguide) "
                 "VALUES(?,?,?,?,?,NULL,NULL) ON CONFLICT(member_key) DO NOTHING", (key, name, chamber, "?", state))


def is_house_member_filer(row) -> bool:
    return row.prefix.startswith("Hon")


_STRIKE_PUT_CALL = re.compile(r"\d\s+(?:put|call)s?(?:\s+assignment)?\s*$", re.I)  # "S&P 500 aUg 19 1725 PUT"
_OPTION_CONTRACT = [
    re.compile(r"^\s*(?:put|call|pu[tx]|ca[lx]{2})/\S+\s*@\s*\d", re.I),     # "PUT/XSP @ 195 EXP 09/16/2016 - ..."
    re.compile(r"@\s*[\d.]+\s+EXP\s+\d{1,2}/\d{1,2}/\d{4}", re.I),         # "... @ 200 EXP 12/20/2019"
    re.compile(r"\b(?:put|call)s?\s+assignment\b", re.I),                  # an option assignment counts as an option
]


def _option_contract(text: str) -> bool:
    return bool(_STRIKE_PUT_CALL.search(text)) or any(p.search(text) for p in _OPTION_CONTRACT)


def _option_description(text: str) -> bool:
    """Whether an old-layout description marks an option trade. Yes: one that starts with PUT or CALL and names a
    strike ("PUT 2/22/2014 $525 (100SHS)", "Put Facebook Inc $75 Exp 04/19/14", "Call Option, $177.50, Exp. 4/5/19"),
    ends with a strike and CALL or PUT ("CoP Jan 15 2016 70.0 Call"), is just "PUT" or "CALL", or is about options
    other than exercising them ("Purchase of 50 Options"). No: "Private equity fund capital call", a preferred
    share's call date "Call 05/15/23@100.00", "Exercise of stock option shares"."""
    starts_put_call = re.match(r"\s*(?:put|call)s?\b", text, re.I) and re.search(r"\$\s?\d", text) and "@" not in text
    only_put_call = re.fullmatch(r"\s*(?:put|call)s?(?:\s+assignment)?\s*", text, re.I)
    names_options = re.search(r"\boptions?\b", text, re.I) and not re.search(r"\bexercis", text, re.I)
    return _is_option("", text) or _option_contract(text) or bool(starts_put_call or only_put_call or names_options)


def _house_is_option(t) -> bool:
    # the pre-2022 layout often has no asset code, so the asset text and an option description count too
    return (t.asset_code == "OP" or _is_option("", t.asset_text) or _option_contract(t.asset_text)
            or _option_description(t.description))


def ingest_house_report(ctx: Ctx, terms: list[Term], row, pdf_path: Path | None, url: str) -> str:
    rid = f"H{row.doc_id}"
    m = match_house(terms, row.first, row.last, row.state_district, row.filed)
    status, reason, trades = "parsed", "", []
    fallback = m.term is None and is_house_member_filer(row) and pdf_path is not None
    member_key = m.term.bioguide if m.term else (f"H-{row.state_district}-{row.last}" if fallback else None)
    if m.term is None and not fallback:
        status, reason = "unmatched", m.reason
    else:
        try:
            trades = house_src.parse_pdf(pdf_path).trades
        except ParseRejected as e:
            status, reason = ("scanned" if str(e) == "scanned" else "rejected"), str(e)
        except _report_errors() as e:                # corrupt PDF, pypdf errors, bad format
            status, reason = "rejected", f"{type(e).__name__}: {e}"
    conn = ctx.conn
    with db.transaction(conn):
        issues.clear_ref(conn, "house", rid)
        if m.term:
            _upsert_member(conn, m.term)
        elif fallback:
            _fallback_member(conn, member_key, f"{row.first} {row.last}", "House", row.state_district[:2])
            issues.record(conn, "house", "member-not-matched", rid,
                          f"{row.first} {row.last} ({row.state_district}): {m.reason}; shown without party", url)
        conn.execute("""INSERT OR REPLACE INTO congress_reports(report_id, chamber, filer_name, member_key, filed, url,
                        status, reason, amends, title) VALUES(?,?,?,?,?,?,?,?,NULL,NULL)""",
                     (rid, "house", f"{row.first} {row.last}", member_key, row.filed.isoformat(), url, status, reason))
        conn.execute("DELETE FROM congress_trades WHERE report_id=?", (rid,))
        conn.executemany("""INSERT INTO congress_trades(report_id, row, owner, asset_text, asset_type, ticker, tx_type,
                            tx_date, amount_text, is_etf, is_option, filing_status)
                            VALUES(?,?,?,?,?,NULL,?,?,?,NULL,?,?)""",
                         [(rid, t.row, t.owner, t.asset_text, t.asset_code, t.tx_type, t.tx_date.isoformat(),
                           t.amount_text, int(_house_is_option(t)), t.filing_status) for t in trades])
        if status != "parsed":
            kind = "unmatched-filer" if status == "unmatched" else status
            issues.record(conn, "house", kind, rid,
                          f"{row.first} {row.last} ({row.state_district}), report {row.doc_id}: {reason}", url)
    return status


def _is_transient(e: Exception) -> bool:
    """Network-level trouble worth retrying on the next run, as opposed to a report that is itself bad."""
    if isinstance(e, HttpError):
        return e.status is None or e.status == 429 or e.status >= 500
    if isinstance(e, (TimeoutError, ConnectionError, OSError)):
        return True
    try:
        from playwright.sync_api import Error as PwError
    except ImportError:
        return False
    return isinstance(e, PwError)          # includes playwright's TimeoutError


def _fetch_failed(ctx: Ctx, source: str, rid: str, label: str, url: str, e: Exception) -> None:
    log.warning("%s report %s fetch failed (will retry): %r", source, rid, e)
    with db.transaction(ctx.conn):
        issues.record(ctx.conn, source, "fetch-failed", rid, f"{label}: {type(e).__name__}: {e}; will retry next run", url)


def _report_errors() -> tuple[type[Exception], ...]:
    from pypdf.errors import PyPdfError
    return (HttpError, ValueError, PyPdfError)


def _reject_house(ctx: Ctx, row, url: str, e: Exception) -> str:
    rid, reason = f"H{row.doc_id}", f"{type(e).__name__}: {e}"
    log.warning("house report %s rejected: %s", rid, reason)
    with db.transaction(ctx.conn):
        issues.clear_ref(ctx.conn, "house", rid)
        ctx.conn.execute("""INSERT OR REPLACE INTO congress_reports(report_id, chamber, filer_name, member_key, filed, url,
                            status, reason, amends, title) VALUES(?,?,?,NULL,?,?,?,?,NULL,NULL)""",
                         (rid, "house", f"{row.first} {row.last}", row.filed.isoformat(), url, "rejected", reason))
        issues.record(ctx.conn, "house", "rejected", rid,
                      f"{row.first} {row.last} ({row.state_district}), report {row.doc_id}: {reason}", url)
    return "rejected"


HOUSE_PARSER_VERSION = 5      # 2: reads the pre-2022 PDF layout; 3: reads each row's filing status (C9);
                              # 4: reads subholding text wrapped onto lines of its own; 5: member matching (C8)
                              # takes another district of the state, late reports, titles and apostrophes


def _reparse_house(ctx: Ctx, where: str, matched_only: bool = False) -> dict:
    """Runs ingest_house_report again for the House reports matching `where`, from the cached PDF and the index row
    in the cached yearly index; the year comes from the report URL. A report missing either, or failing, keeps its
    stored state and gets a Data issue; the others go on. With `matched_only`, only reports whose filer now matches
    a member are read again."""
    rows = ctx.conn.execute(f"SELECT report_id, url FROM congress_reports WHERE chamber='house' AND ({where}) "
                            "ORDER BY report_id").fetchall()
    counts, index, terms = defaultdict(int), {}, None

    def problem(kind: str, rid: str, url: str, message: str) -> None:
        counts[kind] += 1
        log.warning("house report %s not re-parsed: %s", rid, message)
        with db.transaction(ctx.conn):
            issues.record(ctx.conn, "house", f"reparse-{kind}", rid, f"report {rid[1:]}: {message}", url or "")

    for r in rows:
        rid, url = r["report_id"], r["url"]
        doc_id, m = rid[1:], re.search(r"/ptr-pdfs/(\d{4})/", url or "")
        path = ctx.raw / "house" / m[1] / f"{doc_id}.pdf" if m else None
        if path is None or not path.exists():
            problem("missing-pdf", rid, url, "no cached PDF to re-parse after a parser upgrade")
            continue
        try:
            year = int(m[1])
            if year not in index:
                xml = ctx.raw / "house" / f"{year}FD.xml"
                index[year] = {x.doc_id: x for x in house_src.parse_index_xml(xml.read_bytes())} if xml.exists() else {}
            row = index[year].get(doc_id)
            if row is None or row.filed is None:
                problem("missing-index-row", rid, url, f"not in the cached {year} index; not re-parsed")
                continue
            if terms is None:
                terms = _terms(ctx)
            if matched_only and match_house(terms, row.first, row.last, row.state_district, row.filed).term is None:
                continue
            counts[ingest_house_report(ctx, terms, row, path, url)] += 1
        except Exception as e:                       # one bad report must not stop the others
            problem("failed", rid, url, f"re-parse failed: {type(e).__name__}: {e}")
    if rows:
        log.info("house reports re-parsed after a parser upgrade (%s): %s", where, dict(counts))
    if counts.get("parsed"):
        _resolve_new_trades(ctx)
    return dict(counts)


def reprocess_rejected_house(ctx: Ctx) -> dict:
    """Reports an older parser rejected for a layout it could not read ("missing header field ...": every report
    filed before 2022) are parsed again."""
    return _reparse_house(ctx, "status='rejected' AND reason LIKE 'missing header field%'")


def reprocess_parsed_house(ctx: Ctx) -> dict:
    """Reports parsed before the filing status of each row was read (every row stored as 'New') are parsed again."""
    return _reparse_house(ctx, "status='parsed'")


def reprocess_unplaced_text_house(ctx: Ctx) -> dict:
    """Reports rejected for text an older parser could not place ("unparsed text before a transaction: ...", such
    as subholding text wrapped onto a line of its own) are parsed again."""
    return _reparse_house(ctx, "status='rejected' AND reason LIKE 'unparsed text before a transaction%'")


def rematch_members(ctx: Ctx) -> None:
    """After a member matching change: a House report kept under a name-only member (C8) is read again if its filer
    now matches a member. A House report whose filer matched no member has no trades and its PDF was never
    downloaded, so it is dropped and the run reads every year's index again, taking it in as new. A Senate report
    kept under a name-only member is filed under the member it now matches; its listing is not stored, so the name
    comes from the key. Name-only members no report uses any more are removed."""
    _reparse_house(ctx, "member_key IN (SELECT member_key FROM members WHERE bioguide IS NULL)", matched_only=True)
    terms = _terms(ctx)
    for rid, key, filed in ctx.conn.execute("""SELECT r.report_id, r.member_key, r.filed FROM congress_reports r
                                               JOIN members m USING(member_key)
                                               WHERE r.chamber='senate' AND m.bioguide IS NULL""").fetchall():
        last, first = key[2:].rsplit("-", 1)             # sync_senate's key: f"S-{last}-{first}"
        m = match_senate(terms, first, last, date.fromisoformat(filed))
        if m.term:
            with db.transaction(ctx.conn):
                _upsert_member(ctx.conn, m.term)
                ctx.conn.execute("UPDATE congress_reports SET member_key=? WHERE report_id=?", (m.term.bioguide, rid))
                ctx.conn.execute("UPDATE issues SET resolved=1 WHERE source='senate' AND kind='member-not-matched' "
                                 "AND ref=?", (rid,))
    with db.transaction(ctx.conn):
        dropped = ctx.conn.execute("DELETE FROM congress_reports WHERE chamber='house' AND status='unmatched'").rowcount
        if dropped:
            db.set_meta(ctx.conn, "house-catch-up", "1")
        ctx.conn.execute("DELETE FROM members WHERE bioguide IS NULL AND member_key NOT IN "
                         "(SELECT member_key FROM congress_reports WHERE member_key IS NOT NULL)")
    log.info("house: %d reports whose filer matched no member are read again from every year's index", dropped)


def sync_house(ctx: Ctx, years: list[int] | None = None) -> dict:
    today = ctx.now().date()
    version = int(db.get_meta(ctx.conn, "house-parser-version", "1"))
    if version < HOUSE_PARSER_VERSION:
        if version < 3:
            reprocess_parsed_house(ctx)       # first, so reports re-parsed just below are not read twice
        if version < 2:
            reprocess_rejected_house(ctx)
        if version < 4:
            reprocess_unplaced_text_house(ctx)
        if version < 5:
            rematch_members(ctx)
        with db.transaction(ctx.conn):
            db.set_meta(ctx.conn, "house-parser-version", str(HOUSE_PARSER_VERSION))
    catch_up = years is None and bool(db.get_meta(ctx.conn, "house-catch-up"))    # first-run step failed, or rematch_members
    years = years or (list(range(FIRST_YEAR, today.year + 1)) if catch_up else [today.year - 1, today.year])
    retry = {int(m[1]) for (u,) in ctx.conn.execute(
        "SELECT url FROM issues WHERE source='house' AND kind='fetch-failed' AND resolved=0")
        if (m := re.search(r"/ptr-pdfs/(\d{4})/", u or ""))}          # earlier transient failures are re-read
    years = sorted(set(years) | retry)
    terms, client = _terms(ctx), house_src.HouseClient(ctx.raw)
    known = {r[0] for r in ctx.conn.execute("SELECT report_id FROM congress_reports WHERE chamber='house'")}
    counts = defaultdict(int)
    for y in years:
        for row in client.index(y):
            if row.filing_type != "P" or f"H{row.doc_id}" in known:
                continue
            url = house_src.pdf_url(y, row.doc_id)
            if row.filed is None:            # the index lists it without a date: try again when the date appears
                with db.transaction(ctx.conn):
                    issues.record(ctx.conn, "house", "missing-filing-date", f"H{row.doc_id}",
                                  f"{row.first} {row.last} ({row.state_district}), report {row.doc_id}: the House "
                                  f"index gives no filing date; skipped until it does", url)
                counts["missing-filing-date"] += 1
                continue
            wanted = match_house(terms, row.first, row.last, row.state_district, row.filed).term or is_house_member_filer(row)
            try:
                path = client.pdf(y, row.doc_id) if wanted else None
                status = ingest_house_report(ctx, terms, row, path, url)
            except Exception as e:                   # one bad report must not stall the source
                if _is_transient(e):
                    _fetch_failed(ctx, "house", f"H{row.doc_id}", f"{row.first} {row.last}", url, e)
                    counts["fetch-failed"] += 1
                    continue
                if not isinstance(e, _report_errors()):
                    raise
                status = _reject_house(ctx, row, url, e)
            counts[status] += 1
            ctx.progress["house"] = dict(counts)
    if catch_up:
        with db.transaction(ctx.conn):
            ctx.conn.execute("DELETE FROM meta WHERE key='house-catch-up'")
    if counts.get("parsed"):                  # new trades: identify their ETFs now, not at the next daily run
        _resolve_new_trades(ctx)
    return dict(counts)


# ---------- Senate ----------

def _is_option(asset_type: str, text: str) -> bool:
    return asset_type in ("Stock Option", "OP") or bool(re.search(r"\b(call|put)s?\b.*\boptions?\b|\boptions?\b.*\b(call|put)s?\b", text, re.I))


def _apply_senate_supersede(conn) -> None:
    groups = defaultdict(list)
    for r in conn.execute("SELECT report_id, member_key, filer_name, title FROM congress_reports WHERE chamber='senate'"):
        report_for, n = senate_src.parse_title(r["title"] or "")
        if report_for:
            groups[(r["member_key"] or r["filer_name"], report_for)].append((n, r["report_id"]))
    for items in groups.values():
        top = max(n for n, _ in items)
        for n, rid in items:
            if n < top:
                conn.execute("UPDATE congress_reports SET status='superseded', reason='replaced by a later amendment' "
                             "WHERE report_id=?", (rid,))


def _senate_filer_is_senator(listing) -> bool:
    """Only senators are matched to members; a staffer sharing a surname must not be attached to one."""
    return "Senator" in listing.office


SENATE_DENIED_WAIT = timedelta(hours=6)   # the job's own interval: retrying a refusal sooner opens a window each time


def sync_senate(ctx: Ctx) -> dict:
    if not ctx.cfg.senate_enabled:
        return {"skipped": "Senate terms not accepted in config.toml"}
    conn, today = ctx.conn, ctx.now().date()
    if (until := db.get_meta(conn, "senate-denied-until")) and ctx.now() < datetime.fromisoformat(until):
        raise senate_src.AccessDenied(f"efdsearch.senate.gov refused the browser at the last try; not tried again "
                                      f"before {datetime.fromisoformat(until):%Y-%m-%d %H:%M} (New York time)")
    since = date.fromisoformat(db.get_meta(conn, "senate-from", f"{FIRST_YEAR}-01-01"))
    terms = _terms(ctx)
    known = {r[0] for r in conn.execute("SELECT report_id FROM congress_reports WHERE chamber='senate'")}
    counts, failed_filed = defaultdict(int), []
    with ExitStack() as stack:
        try:
            client = stack.enter_context(senate_src.SenateClient(visible=ctx.cfg.senate_headed))
        except senate_src.AccessDenied:
            with db.transaction(conn):
                db.set_meta(conn, "senate-denied-until", (ctx.now() + SENATE_DENIED_WAIT).isoformat())
            raise
        listings = list({l.url: l for l in client.search(max(date(FIRST_YEAR, 1, 1), since - timedelta(days=30)))}.values())
        for l in listings:
            rid = "S" + l.url.rstrip("/").rsplit("/", 1)[-1]
            if rid in known:
                continue
            is_senator = _senate_filer_is_senator(l)
            m = match_senate(terms, l.first, l.last, l.filed) if is_senator else None
            status, reason, trades = "parsed", "", []
            term = m.term if m else None
            fallback = is_senator and term is None
            member_key = term.bioguide if term else (f"S-{l.last}-{l.first}" if fallback else None)
            if term is None and not fallback:
                status, reason = "unmatched", "filer is not a senator"
            elif l.kind == "paper":
                status, reason = "scanned", "paper report (scanned images)"
            else:
                try:
                    page = client.page(l.url)
                    (ctx.raw / "senate").mkdir(parents=True, exist_ok=True)
                    (ctx.raw / "senate" / f"{rid}.html").write_text(page)
                    trades = senate_src.parse_ptr_html(page)
                except ParseRejected as e:
                    status, reason = "rejected", str(e)
                except Exception as e:               # one bad listing must not stall the source
                    if _is_transient(e):
                        _fetch_failed(ctx, "senate", rid, f"{l.first} {l.last}, {l.title}", l.url, e)
                        counts["fetch-failed"] += 1
                        failed_filed.append(l.filed)
                        continue
                    if not isinstance(e, _report_errors()):
                        raise
                    status, reason = "rejected", f"{type(e).__name__}: {e}"
            try:
                with db.transaction(conn):
                    issues.clear_ref(conn, "senate", rid)
                    if term:
                        _upsert_member(conn, term)
                    elif fallback:
                        _fallback_member(conn, member_key, f"{l.first} {l.last}", "Senate", "")
                        issues.record(conn, "senate", "member-not-matched", rid,
                                      f"{l.first} {l.last}: {m.reason}; shown without party or state", l.url)
                    conn.execute("""INSERT OR REPLACE INTO congress_reports(report_id, chamber, filer_name, member_key, filed,
                                    url, status, reason, amends, title) VALUES(?,?,?,?,?,?,?,?,NULL,?)""",
                                 (rid, "senate", f"{l.first} {l.last}", member_key, l.filed.isoformat(), l.url, status,
                                  reason, l.title))
                    conn.execute("DELETE FROM congress_trades WHERE report_id=?", (rid,))
                    conn.executemany("""INSERT INTO congress_trades(report_id, row, owner, asset_text, asset_type, ticker,
                                        tx_type, tx_date, amount_text, is_etf, is_option) VALUES(?,?,?,?,?,?,?,?,?,NULL,?)""",
                                     [(rid, t.row, t.owner, t.asset_name, t.asset_type,
                                       None if t.ticker in ("", "--") else t.ticker, t.tx_type, t.tx_date.isoformat(),
                                       t.amount_text, int(_is_option(t.asset_type, t.asset_name))) for t in trades])
                    if status != "parsed":
                        kind = "unmatched-filer" if status == "unmatched" else status
                        issues.record(conn, "senate", kind, rid, f"{l.first} {l.last}, {l.title}: {reason}", l.url)
            except sqlite3.IntegrityError as e:       # one report's rows must not abort the whole job
                log.warning("senate report %s could not be stored: %s", rid, e)
                with db.transaction(conn):
                    issues.record(conn, "senate", "senate-report-error", rid,
                                  f"{l.first} {l.last}, {l.title}: could not be stored ({e})", l.url)
                counts["integrity-failed"] += 1
                continue
            counts[status] += 1
            ctx.progress["senate"] = dict(counts)
    with db.transaction(conn):
        _apply_senate_supersede(conn)
        db.set_meta(conn, "senate-from", min([today, *failed_filed]).isoformat())   # failed listings are searched again
    if counts.get("parsed"):                  # new trades: identify their ETFs now, not at the next daily run
        _resolve_new_trades(ctx)
    return dict(counts)


# ---------- Congress tickers ----------

def _resolve_new_trades(ctx: Ctx) -> None:
    """Called after new reports were stored. The reports are safely in; if identification fails here (for example
    the reference files are not downloaded yet), the daily reference job does it, so the sync itself still succeeds."""
    try:
        resolve_congress_tickers(ctx)
    except Exception:
        log.exception("ticker identification after new reports failed; the daily reference job will retry")


def _known_non_etp(candidates, figi_types: dict) -> bool:
    return any(figi_types.get(t) not in (None, "", "ETP") for t in candidates)


def reevaluate_possible_etf_issues(conn, names: dict | None = None) -> dict:
    """One-time, offline: resolve open 'unmatched-possible-etf' issues the current rule would not raise (rule v2)."""
    if int(db.get_meta(conn, "etf-issue-rule-version") or 0) >= 2:
        return {"skipped": "already done"}
    figi_types = {r[0]: r[1] for r in conn.execute("SELECT ticker, figi_type FROM securities")}
    rows = conn.execute("""SELECT i.id, t.asset_text, t.ticker, t.is_option FROM issues i
                           JOIN congress_trades t ON i.ref = t.report_id || '#' || t.row
                           WHERE i.source='congress' AND i.kind='unmatched-possible-etf' AND i.resolved=0""").fetchall()
    resolved = 0
    with db.transaction(conn):
        for r in rows:
            c = ticker_candidates(r["asset_text"], None)
            if not c and names and (m := match_by_name(r["asset_text"], names)):
                c = [m]
            c = set(c) | ({r["ticker"]} if r["ticker"] else set())
            if r["is_option"] or not looks_like_etf(r["asset_text"]) or _known_non_etp(c, figi_types):
                conn.execute("UPDATE issues SET resolved=1 WHERE id=?", (r["id"],))
                resolved += 1
        db.set_meta(conn, "etf-issue-rule-version", "2")
    return {"checked": len(rows), "resolved": resolved}


def resolve_congress_tickers(ctx: Ctx) -> dict:
    conn, names = ctx.conn, _etf_names(ctx)
    reevaluate_possible_etf_issues(conn, names)
    rows = conn.execute("""SELECT t.report_id, t.row, t.asset_text, t.ticker, t.is_option, r.url FROM congress_trades t
                           JOIN congress_reports r USING(report_id)
                           WHERE t.is_etf IS NULL AND r.status = 'parsed'""").fetchall()
    cands = {}
    for r in rows:
        c = ticker_candidates(r["asset_text"], r["ticker"])
        if not c and (m := match_by_name(r["asset_text"], names)):
            c = [m]
        cands[(r["report_id"], r["row"])] = c
    etf = ensure_securities(ctx, {t for c in cands.values() for t in c})
    figi_types = {r[0]: r[1] for r in conn.execute("SELECT ticker, figi_type FROM securities")}
    n_etf = 0
    with db.transaction(conn):
        for r in rows:
            c = cands[(r["report_id"], r["row"])]
            hit = next((t for t in c if etf.get(t)), None)
            ref = f"{r['report_id']}#{r['row']}"
            issues.clear_ref(conn, "congress", ref)
            conn.execute("UPDATE congress_trades SET ticker=?, is_etf=? WHERE report_id=? AND row=?",
                         (hit or (c[0] if c else None), int(hit is not None), r["report_id"], r["row"]))
            n_etf += hit is not None
            if hit is None and not r["is_option"] and looks_like_etf(r["asset_text"]) and not _known_non_etp(
                    c, figi_types):   # OpenFIGI knows it as a non-ETP security
                issues.record(conn, "congress", "unmatched-possible-etf", ref,
                              f"could not match {r['asset_text']!r} to exactly one ETF", r["url"])
    return {"checked": len(rows), "etf_trades": n_etf}


# ---------- SEC 13F ----------

def oldest_period(today: date) -> date:
    """Quarter-end on or before the earliest 5-year window start (with a week of margin)."""
    try:
        five = today.replace(year=today.year - 5)
    except ValueError:
        five = today.replace(year=today.year - 5, day=28)
    return last_quarter_end_on_or_before(five - timedelta(days=7))


def _map_cusips(ctx: Ctx, cusips: set[str]) -> dict[str, tuple[str | None, str | None]]:
    conn = ctx.conn
    known = {r["cusip"]: (r["ticker"], r["figi_type"]) for r in conn.execute("SELECT cusip, ticker, figi_type FROM cusip_map")}
    todo = sorted(c for c in cusips if c not in known)
    for i in range(0, len(todo), 1000):
        chunk = todo[i:i + 1000]
        hits = ctx.figi.map_cusips(chunk)
        with db.transaction(conn):
            for c, h in zip(chunk, hits):
                conn.execute("INSERT OR REPLACE INTO cusip_map(cusip, ticker, figi_type, exch, raw, checked_at) VALUES(?,?,?,?,?,?)",
                             (c, h.ticker if h else None, h.security_type if h else None, h.exch if h else None,
                              json.dumps(h.__dict__ if h else None), _now_iso(ctx)))
                known[c] = (h.ticker, h.security_type) if h else (None, None)
        ctx.progress["cusips"] = f"{min(i + 1000, len(todo))}/{len(todo)}"
    return known


def _ingest_13f(ctx: Ctx, filings: dict[str, sec13f.Filing], rows_fn, totals: dict[str, tuple[int, float]]) -> dict:
    conn = ctx.conn
    cusips, books = set(), dict.fromkeys(filings, 0.0)     # book: the value of every share row of the filing
    for h in rows_fn():
        cusips.add(h.cusip)
        if h.accession in books:
            books[h.accession] += h.value_usd
    cmap = _map_cusips(ctx, cusips)
    etp = {cmap[c][0] for c in cusips if cmap[c][1] == "ETP" and cmap[c][0]}
    etf = ensure_securities(ctx, etp)
    etf_cusips = {c for c in cusips if cmap[c][0] and etf.get(cmap[c][0])}
    unknown = {c for c in cusips if cmap[c][0] is None}
    keep = sec13f.aggregate(h for h in rows_fn() if h.cusip in etf_cusips or (
        h.cusip in unknown and re.search(r"\bETF\b", f"{h.issuer} {h.title}", re.I)))
    with db.transaction(conn):
        conn.executemany("INSERT OR REPLACE INTO f13_filings(accession, cik, filer_name, period, filed, form, amendment_type) "
                         "VALUES(?,?,?,?,?,?,?)",
                         [(f.accession, f.cik, f.filer_name, f.period.isoformat(), f.filed.isoformat(), f.form,
                           f.amendment_type) for f in filings.values()])
        conn.executemany("INSERT OR REPLACE INTO f13_holdings(accession, cusip, shares, value_usd, issuer, title) "
                         "VALUES(?,?,?,?,?,?)",
                         [(h.accession, h.cusip, h.shares, h.value_usd, h.issuer, h.title)
                          for hs in keep.values() for h in hs.values()])
        conn.executemany("INSERT OR REPLACE INTO f13_filing_totals(accession, n_rows, total_value_usd) VALUES(?,?,?)",
                         [(a, n, v) for a, (n, v) in totals.items()])
        conn.executemany("INSERT OR REPLACE INTO f13_books(accession, book_usd) VALUES(?,?)", books.items())
        if unknown:
            etf_named = sum(1 for hs in keep.values() for h in hs.values() if h.cusip in unknown)
            issues.record(conn, "sec13f", "unidentified-cusips", f"batch-{min(filings)}",
                          f"{len(unknown)} CUSIPs not found on OpenFIGI; {etf_named} holdings named like ETFs are "
                          f"counted as unpriceable ETFs")
    return {"filings": len(filings), "etf_cusips": len(etf_cusips)}


def ingest_dataset(ctx: Ctx, ds: sec13f.DataSet, oldest: date) -> dict:
    filings = {a: f for a, f in sec13f.read_filings(ds).items() if f.period >= oldest}
    return _ingest_13f(ctx, filings, lambda: sec13f.iter_share_rows(ds, filings), sec13f.filing_totals(ds, filings))


def backfill_13f_books(ctx: Ctx) -> dict:
    """Once, for filings stored before f13_books existed. A filing from an SEC data set gets its book summed again
    from the cached data set (its stored total is the summary page's, which also counts options and principal
    amounts and is sometimes entered in other units). A filing from EDGAR already has the book as its stored total
    (sync_13f_new sums its share rows), but that is used only when every data set ever ingested was read again:
    only then are the remaining filings known to be EDGAR ones. Anything else stays unknown, with a Data issue."""
    conn = ctx.conn
    if db.get_meta(conn, "13f-books"):
        return {}
    known = {r[0] for r in conn.execute("SELECT accession FROM f13_filings")}
    covered, missing = set(), []
    for (key,) in conn.execute("SELECT key FROM meta WHERE key LIKE '13f-ds:%' ORDER BY key").fetchall():
        url = key[len("13f-ds:"):]
        path = ctx.raw / "sec13f" / url.rsplit("/", 1)[-1]
        if not path.exists():
            missing.append(url)
            continue
        ctx.progress["sec13f"] = f"book totals from {path.name}"
        ds = sec13f.DataSet(path)
        filings = {a: f for a, f in sec13f.read_filings(ds).items() if a in known}
        books = dict.fromkeys(filings, 0.0)
        for h in sec13f.iter_share_rows(ds, filings):
            books[h.accession] += h.value_usd
        with db.transaction(conn):
            conn.executemany("INSERT OR REPLACE INTO f13_books(accession, book_usd) VALUES(?,?)", books.items())
        covered |= books.keys()
    from_edgar = 0
    with db.transaction(conn):
        if missing:
            issues.record(conn, "sec13f", "book-unknown", "data-sets",
                          f"{len(missing)} ingested 13F data set(s) are no longer cached ({', '.join(missing)}); "
                          f"ETF share of the book stays unknown for filings not read again")
        else:
            have = {r[0] for r in conn.execute("SELECT accession FROM f13_books")}
            totals = {r[0]: r[1] for r in conn.execute("SELECT accession, total_value_usd FROM f13_filing_totals")}
            rest = [(a, totals.get(a) or 0.0) for a in sorted(known - covered - have)]   # no totals: no share rows
            conn.executemany("INSERT INTO f13_books(accession, book_usd) VALUES(?,?)", rest)
            from_edgar = len(rest)
        db.set_meta(conn, "13f-books", "1")
    ctx.progress.pop("sec13f", None)
    return {"from_data_sets": len(covered), "from_edgar_totals": from_edgar}


def sync_13f_datasets(ctx: Ctx) -> dict:
    conn, oldest = ctx.conn, oldest_period(ctx.now().date())
    backfill_13f_books(ctx)
    urls = sec13f.list_dataset_urls(ctx.sec(DATASETS_PAGE).decode("utf-8", "replace"))
    done = 0
    for url in reversed(urls):
        try:
            _, end = sec13f.dataset_range(url)
        except ValueError as e:
            with db.transaction(conn):
                issues.record(conn, "sec13f", "dataset-name-unrecognized", url, str(e), url)
            continue
        if end < oldest or db.get_meta(conn, "13f-ds:" + url):
            continue
        path = _download(url, ctx.raw / "sec13f" / url.rsplit("/", 1)[-1], ctx.sec)
        ctx.progress["sec13f"] = f"data set {url.rsplit('/', 1)[-1]}"
        ingest_dataset(ctx, sec13f.DataSet(path), oldest)
        with db.transaction(conn):
            db.set_meta(conn, "13f-ds:" + url, "done")
            nxt = (end + timedelta(days=1)).isoformat()
            if nxt > (db.get_meta(conn, "13f-idx-from") or ""):
                db.set_meta(conn, "13f-idx-from", nxt)
        done += 1
    return {"datasets": done}


def _totals(rows) -> dict[str, tuple[int, float]]:
    out: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for h in rows:
        t = out[h.accession]
        t[0] += 1
        t[1] += h.value_usd
    return {a: (n, v) for a, (n, v) in out.items()}


def sync_13f_new(ctx: Ctx) -> dict:
    conn, today = ctx.conn, ctx.now().date()
    oldest = oldest_period(today)
    seen = {r[0] for r in conn.execute("SELECT accession FROM f13_filings")}
    entries: dict[str, tuple[str, str, date]] = {}
    d = date.fromisoformat(db.get_meta(conn, "13f-idx-from") or today.isoformat())
    resume = today
    while d < today:
        if d.weekday() < 5:
            url = DAILY_IDX.format(y=d.year, q=(d.month - 1) // 3 + 1, ymd=d.strftime("%Y%m%d"))
            try:
                text = ctx.sec(url).decode("latin-1")
            except HttpError as e:
                if e.status in (403, 404) and (today - d).days <= 3:
                    resume = d                       # not published yet: retry from here next time
                    break
                if e.status not in (403, 404):
                    raise
                text = ""                            # holiday
            for e in sec13f.parse_form_idx(text):
                if e.form in sec13f.FORMS:
                    entries[sec13f.accession_from_path(e.path)] = (e.form, e.cik, e.filed)
        d += timedelta(days=1)
    for form in ("13F-HR", "13F-HR/A"):
        for f, cik, acc, filed in sec13f.parse_current_atom(ctx.sec(CURRENT_FEED.format(form=form))):
            entries.setdefault(acc, (f, cik, filed))
    filings, rows, out = {}, [], {"filings": 0}

    def flush():
        nonlocal filings, rows, out
        if filings:                                   # batches: a crash resumes cheaply (seen skips ingested ones)
            r = _ingest_13f(ctx, filings, lambda part=rows: iter(part), _totals(rows))
            out = {k: out.get(k, 0) + v for k, v in r.items()}
        filings, rows = {}, []

    for acc, (form, cik, filed) in entries.items():
        if acc in seen:
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"
        try:
            names = [i["name"] for i in json.loads(ctx.sec(base + "index.json"))["directory"]["item"]]
            if "primary_doc.xml" not in names:
                continue
            f = sec13f.parse_primary_doc(ctx.sec(base + "primary_doc.xml"), acc, cik, filed, form)
            if f.period < oldest:
                continue
            info = None
            for n in names:
                if n.lower().endswith(".xml") and n != "primary_doc.xml":
                    body = ctx.sec(base + n)
                    if b"informationTable" in body:
                        info = body
                        break
            held = sec13f.parse_infotable_xml(info, f) if info is not None else None
        except (HttpError, ParseError, ValueError, KeyError, TypeError) as e:
            if _is_transient(e):                     # network or server trouble: the whole run is retried
                raise
            with db.transaction(conn):               # one bad filing must not stop the others
                issues.record(conn, "sec13f", "filing-unreadable", acc, f"{type(e).__name__}: {e}; skipped", base)
            continue
        if held is None:
            with db.transaction(conn):
                issues.record(conn, "sec13f", "no-infotable", acc, f"{f.filer_name}: filing has no information table", base)
            continue
        filings[acc] = f
        rows += held
        if len(filings) >= 200:
            flush()
    flush()
    with db.transaction(conn):
        db.set_meta(conn, "13f-idx-from", resume.isoformat())
    return out


# ---------- leaderboards ----------

def _etf_tickers(conn) -> dict[str, str]:
    """CUSIP -> ticker for CUSIPs whose ticker is a confirmed ETF (is_etf = 1)."""
    return {r[0]: r[1] for r in conn.execute("""SELECT m.cusip, m.ticker FROM cusip_map m
                                                JOIN securities s ON s.ticker = m.ticker WHERE s.is_etf = 1""")}


def _merge_positions(p: Position, h: Position) -> Position:
    """F2 NEW HOLDINGS: adds to the earlier row of the same CUSIP (as sec13f does for its own Holding rows)."""
    return replace(p, shares=p.shares + h.shares, reported_usd=p.reported_usd + h.reported_usd)


def _histories(conn, ciks: list[str], etf_tickers: dict[str, str] | None = None
               ) -> tuple[list[FilerHistory], list[str]]:
    etf_tickers = _etf_tickers(conn) if etf_tickers is None else etf_tickers
    cur = conn.cursor()
    cur.row_factory = None                          # plain tuples: this reads every stored holding of the filers
    q = ",".join("?" * len(ciks))
    filings = [sec13f.Filing(acc, cik, name or "", date.fromisoformat(period), date.fromisoformat(filed), form,
                             amendment or "")
               for acc, cik, name, period, filed, form, amendment
               in cur.execute(f"SELECT accession, cik, filer_name, period, filed, form, amendment_type "
                              f"FROM f13_filings WHERE cik IN ({q})", ciks)]
    accs = [f.accession for f in filings]
    by_acc: dict[str, dict[str, Position]] = defaultdict(dict)       # rows straight into leaderboard positions
    for i in range(0, len(accs), 900):
        part = accs[i:i + 900]
        for acc, cusip, shares, value, issuer in cur.execute(
                f"""SELECT accession, cusip, shares, value_usd, issuer FROM f13_holdings
                    WHERE accession IN ({','.join('?' * len(part))})""", part):
            by_acc[acc][cusip] = Position(cusip, etf_tickers.get(cusip), shares, value, issuer or "")
    eff, unknown = sec13f.effective_holdings(filings, by_acc, merge=_merge_positions)
    latest_acc = {}
    for f in sorted(filings, key=lambda f: (f.filed, f.accession)):
        latest_acc[(f.cik, f.period)] = f.accession
    names = {f.cik: f.filer_name for f in sorted(filings, key=lambda f: f.filed)}
    by_cik: dict[str, list] = defaultdict(list)
    for (k, p), hs in eff.items():
        by_cik[k].append((p, hs))
    out = []
    for cik in ciks:
        periods = {p: list(hs.values()) for p, hs in by_cik.get(cik, [])}
        if periods:
            out.append(FilerHistory(cik, names.get(cik, cik), periods,
                                    {p: latest_acc[(cik, p)] for p in periods}))
    return out, unknown


def _reported_etf_totals(conn) -> dict[str, dict[date, float]]:
    """Per filer and period with an effective report (F2: an original, a restatement or new holdings), an upper
    bound on its reported ETF value: the sum of max(value, 0) over every stored row of every filing for that period.
    Used only to skip filers that cannot reach the F8 listing value (see leaderboards.REPORTED_FLOOR_USD)."""
    out: dict[str, dict[date, float]] = defaultdict(dict)
    for r in conn.execute("""SELECT f.cik, f.period, SUM(MAX(COALESCE(h.value_usd, 0), 0)) AS reported
                             FROM f13_filings f LEFT JOIN f13_holdings h ON h.accession = f.accession
                             GROUP BY f.cik, f.period
                             HAVING MAX(f.form = '13F-HR' OR f.amendment_type IN ('RESTATEMENT', 'NEW HOLDINGS')) = 1"""):
        out[r["cik"]][date.fromisoformat(r["period"])] = r["reported"]
    return out


def _reused_tickers(conn) -> dict[str, tuple[str, str]]:
    """E4: ETF tickers whose price history starts after a trade reported under them -> (security name, first day)."""
    rows = conn.execute("""SELECT p.ticker, COALESCE(s.name, p.ticker), p.first FROM
                             (SELECT ticker, MIN(day) AS first FROM prices_daily WHERE ticker IN
                                (SELECT DISTINCT ticker FROM congress_trades WHERE is_etf=1) GROUP BY ticker) p
                           LEFT JOIN securities s USING(ticker)
                           WHERE EXISTS (SELECT 1 FROM congress_trades t WHERE t.ticker=p.ticker AND t.is_etf=1
                                         AND t.tx_date < p.first)""")
    return {t: (name, first) for t, name, first in rows}


def _congress_inputs(conn, service: dict[str, tuple] | None = None
                     ) -> tuple[dict[str, MemberInfo], dict[str, list[CTrade]], list[tuple[str, str, str, str]]]:
    """Each member's countable ETF trades, in filing order (C9). A "Deleted" row removes the earlier trade it names
    and an "Amended" row replaces it; identical trades on two reports count once. A trade under a reused ticker counts
    only if its text names an ETF (E4). `service` is each member key's (bioguide's) times in office (C11). Problems are
    (kind, ref, message, report URL)."""
    service = service or {}
    members = {r["member_key"]: MemberInfo(r["member_key"], r["name"], r["chamber"], r["party"], r["state"],
                                           service.get(r["member_key"], ()))
               for r in conn.execute("SELECT * FROM members")}   # fallback members have party '?' (C8)
    reused = _reused_tickers(conn)
    live: dict[str, list[tuple[tuple, CTrade]]] = defaultdict(list)    # member -> [((ticker, tx, kind, owner), trade)]
    problems, seen = [], {}

    def drop(member: str, i: int) -> None:
        (ticker, tx, kind, owner), t = live[member].pop(i)
        key = (member, ticker, tx, kind, owner, t.amount_text)
        if seen.get(key) == t.report_id:            # an identical trade filed later may count again
            del seen[key]

    for r in conn.execute("""SELECT t.*, r.member_key, r.filed, r.url FROM congress_trades t JOIN congress_reports r
                             USING(report_id) WHERE r.status='parsed' AND t.is_etf=1 AND t.is_option=0
                             ORDER BY r.filed, t.report_id, t.row"""):
        ref = f"{r['report_id']}#{r['row']}"
        if r["ticker"] in reused and not looks_like_etf(r["asset_text"] or ""):
            name, first = reused[r["ticker"]]
            problems.append(("reused-ticker", ref, f"{r['ticker']} is now {name}, whose prices start on {first}, after "
                             f"trades reported under {r['ticker']}; {r['asset_text']!r} does not name an ETF, so it is "
                             f"not counted as one (E4)", r["url"]))
            continue
        kind = TX_KIND.get(r["tx_type"])
        if kind is None:
            problems.append(("congress-trade-skipped", ref, f"{r['tx_type']} transaction in {r['ticker']} skipped (C4)",
                             r["url"]))
            continue
        try:
            amount = parse_amount(r["amount_text"])
        except ValueError:
            problems.append(("congress-trade-skipped", ref, f"unreadable amount {r['amount_text']!r}", r["url"]))
            continue
        filed, tx = date.fromisoformat(r["filed"]), date.fromisoformat(r["tx_date"])
        if tx > filed:
            problems.append(("congress-trade-skipped", ref,
                             f"transaction date {tx} is after the filing date {filed}; skipped", r["url"]))
            continue
        member, key4, status = r["member_key"], (r["ticker"], tx, kind, r["owner"]), r["filing_status"] or "New"
        what = f"{kind} of {r['ticker']} on {tx} ({r['amount_text']}, owner {r['owner']})"
        earlier = [i for i, (k, t) in enumerate(live[member]) if k == key4 and t.report_id != r["report_id"]]
        if status == "Deleted":                     # never counted itself
            exact = [i for i in earlier if live[member][i][1].amount_text == r["amount_text"]]
            pick = exact[-1] if exact else (earlier[0] if len(earlier) == 1 else None)
            if pick is None:
                problems.append(("deleted-trade-unmatched", ref, f"deleted {what} matches no single earlier trade; "
                                                                 f"nothing removed", r["url"]))
            else:
                if not exact:                       # informational: the amounts differ
                    gone = live[member][pick][1]
                    problems.append(("deleted-trade-fuzzy-match", ref, f"deleted {what} removed "
                                     f"{gone.report_id}#{gone.row} with amount {gone.amount_text}", r["url"]))
                drop(member, pick)
            continue
        if status == "Amended":
            if len(earlier) == 1:
                drop(member, earlier[0])
            else:
                problems.append(("amended-trade-unmatched", ref, f"amended {what} matches {len(earlier)} earlier "
                                                                 f"trades, not exactly one; counted as a new trade",
                                 r["url"]))
        key = (member, *key4, r["amount_text"])
        if key in seen and seen[key] != r["report_id"]:      # C9: the same trade on a second report counts once
            continue
        seen[key] = r["report_id"]
        live[member].append((key4, CTrade(r["report_id"], r["row"], filed, tx, r["ticker"], kind, amount,
                                          r["amount_text"], r["url"])))
    trades = {m: [t for _, t in v] for m, v in live.items() if v}
    return members, trades, problems


_JSON = {"check_circular": False, "separators": (",", ":")}


class _PeriodStore:
    """13F investor periods (each quarter's positions) are almost all of the detail bytes and rarely change from one
    rebuild to the next, so each distinct period is stored once in board_periods under a hash of its JSON, and a
    detail row lists its periods by hash. Only periods not stored yet are staged; every hash in use is recorded, so
    the final transaction can drop the ones no row uses any more."""

    def __init__(self, conn):
        self.stored = {r[0] for r in conn.execute("SELECT h FROM board_periods")}
        self.new: dict[str, str] = {}
        self.refs: set[str] = set()

    def hashes(self, periods: list[dict]) -> list[str]:
        out = []
        for p in periods:
            js = json.dumps(p, **_JSON)
            h = hashlib.blake2b(js.encode(), digest_size=16).hexdigest()
            if h not in self.stored and h not in self.new:
                self.new[h] = js
            self.refs.add(h)
            out.append(h)
        return out


def _stage_details(conn, group: str, win: str, rows: list[dict], periods: _PeriodStore | None = None) -> None:
    """Move each row's detail out of the board: the whole row (detail included) is encoded one at a time into a
    TEMP staging table, and `detail` is dropped from the row, so boards are kept and stored without details.
    The staging table lives in this connection's temp database, so this takes no lock on the main database; the
    final transaction of build_boards copies it into board_details. The first row with an id wins, as /api/person
    picked the first match (ranked before unranked). With `periods`, a detail's periods are staged in it and the
    row keeps their hashes (see _PeriodStore and read_detail)."""
    def encoded():
        for r in rows:
            d = r.get("detail")
            if periods is not None and d and d.get("periods"):
                d["periods"] = periods.hashes(d["periods"])
            yield group, win, str(r["id"]), json.dumps(r, **_JSON)
            r.pop("detail", None)
    conn.execute("BEGIN")
    try:
        conn.executemany("INSERT OR IGNORE INTO temp.board_details_new(grp, win, id, json) VALUES(?,?,?,?)", encoded())
        if periods is not None:
            conn.executemany("INSERT OR IGNORE INTO temp.board_periods_new(h, json) VALUES(?,?)", periods.new.items())
            conn.executemany("INSERT OR IGNORE INTO temp.board_period_refs(h) VALUES(?)", ((h,) for h in periods.refs))
            periods.stored |= periods.new.keys()
            periods.new.clear()
            periods.refs.clear()
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def read_detail(conn, group: str, win: str, id_: str) -> dict | None:
    """A stored board row with its detail, its periods read back from board_periods (rows stored before that table
    keep theirs inline). One read transaction, so a rebuild finishing in between cannot remove periods it lists."""
    conn.execute("BEGIN")
    try:
        row = conn.execute("SELECT json FROM board_details WHERE grp=? AND win=? AND id=?",
                           (group, win, id_)).fetchone()
        if row is None:
            return None
        full = json.loads(row[0])
        refs = (full.get("detail") or {}).get("periods")
        if refs and isinstance(refs[0], str):
            wanted = sorted(set(refs))
            stored = {h: js for h, js in conn.execute(
                f"SELECT h, json FROM board_periods WHERE h IN ({','.join('?' * len(wanted))})", wanted)}
            full["detail"]["periods"] = [json.loads(stored[h]) for h in refs]
        return full
    finally:
        conn.execute("COMMIT")


def refresh_13f_stats(conn, now: datetime) -> None:
    """Per filer and quarter, the reported ETF value and the book (see f13_books) under F2: the original or the
    latest restatement, plus NEW HOLDINGS amendments, in the order effective_holdings applies them. The book is
    unknown if any filing behind it has none. Depends on filings only, so data rebuilds write it and live ones read
    it (see build_boards)."""
    etf = {a: v for a, v in conn.execute("SELECT accession, SUM(value_usd) FROM f13_holdings GROUP BY accession")}
    book = {a: v for a, v in conn.execute("SELECT accession, book_usd FROM f13_books")}
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for acc, cik, period, filed, form, amend in conn.execute(
            "SELECT accession, cik, period, filed, form, COALESCE(amendment_type, '') FROM f13_filings"):
        groups[(cik, period)].append((filed, form != "13F-HR", acc, form, amend))
    rows = []
    for (cik, period), fs in groups.items():
        cur = None
        for _, _, acc, form, amend in sorted(fs):
            e, b = etf.get(acc, 0.0), book.get(acc)
            if form == "13F-HR" or amend == "RESTATEMENT":
                cur = (e, b)
            elif amend == "NEW HOLDINGS":
                e0, b0 = cur or (0.0, 0.0)
                cur = (e0 + e, None if b0 is None or b is None else b0 + b)
        if cur is not None:
            rows.append((cik, period, *cur))
    with db.transaction(conn):
        conn.execute("DELETE FROM f13_period_stats")
        conn.executemany("INSERT INTO f13_period_stats(cik, period, etf_usd, book_usd) VALUES(?,?,?,?)", rows)
        db.set_meta(conn, "13f-stats-at", now.isoformat())


def etf_shares(conn) -> dict[str, dict[date, float | None]]:
    """cik -> quarter-end -> reported ETF value / book, or None if the book is unknown or zero."""
    out: dict[str, dict[date, float | None]] = defaultdict(dict)
    for cik, period, e, b in conn.execute("SELECT cik, period, etf_usd, book_usd FROM f13_period_stats"):
        out[cik][date.fromisoformat(period)] = e / b if b is not None and b > 0 else None
    return dict(out)


_BOARDS_LOCK = threading.Lock()          # the job thread and the live thread both rebuild; never at the same time


def build_boards(ctx: Ctx, managers_path: Path = MANAGERS_PATH, famous_path: Path = FAMOUS_PATH,
                 live: bool = False) -> dict:
    """`live`: a rebuild for new live prices only. It reads the stats that depend on filings alone (13F ETF share
    of the book) instead of computing them; a data rebuild computes them."""
    with _BOARDS_LOCK:
        return _build_boards(ctx, managers_path, famous_path, live)


def _service(ctx: Ctx) -> dict[str, tuple]:
    """C11 times in office. Before the reference files are first downloaded no Congress trade can be stored yet."""
    try:
        return service_periods(_terms(ctx))
    except FileNotFoundError:
        if ctx.conn.execute("SELECT 1 FROM congress_trades LIMIT 1").fetchone():
            raise
        return {}


def _build_boards(ctx: Ctx, managers_path: Path, famous_path: Path, live: bool = False) -> dict:
    conn, now = ctx.conn, ctx.now()
    today = now.date()
    if not live or db.get_meta(conn, "13f-stats-at") is None:
        refresh_13f_stats(conn, now)
    shares = etf_shares(conn)
    managers = load_managers(managers_path, _curation_today(ctx))
    famous_list = load_famous(famous_path, _curation_today(ctx))
    famous = {f.cik: (f.person, f.role) for f in famous_list}
    famous_names = {f.cik: f.filer_name for f in famous_list}
    book = load_book(ctx, tracked_tickers(conn, managers_path))
    if "SPY" not in book.tri:
        raise RuntimeError("SPY price history missing; run the price sync first")
    if not live:                                  # Senate data changes only with a data job
        with db.transaction(conn):
            _apply_senate_supersede(conn)
    members, ctrades, problems = _congress_inputs(conn, _service(ctx))
    ciks = [r[0] for r in conn.execute("""SELECT DISTINCT f.cik FROM f13_filings f JOIN f13_filing_totals t USING(accession)""")]
    windows = {k: make_window(k, book, today) for k in WINDOWS}
    reported = _reported_etf_totals(conn)
    every = set(ciks) | set(famous)
    ciks = sorted({c for c in ciks if not all(cannot_be_listed(reported.get(c, {}), w, today) for w in windows.values())}
                  | set(famous))
    boards, x2_issues = {}, []
    skipped = every - set(ciks)               # their histories are not loaded; still report their unknown amendments
    unknown_amend = {r[0] for r in conn.execute(
        """SELECT accession, cik FROM f13_filings WHERE form != '13F-HR'
           AND COALESCE(amendment_type, '') NOT IN ('RESTATEMENT', 'NEW HOLDINGS')""") if r[1] in skipped}
    spy = {k: simple_return(book.tri["SPY"], w.start, w.end) for k, w in windows.items()}
    inv = {k: ([], []) for k in WINDOWS}
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS board_details_new "
                 "(grp TEXT, win TEXT, id TEXT, json TEXT NOT NULL, PRIMARY KEY (grp, win, id))")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS board_periods_new (h TEXT PRIMARY KEY, json TEXT NOT NULL)")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS board_period_refs (h TEXT PRIMARY KEY)")
    for t in ("board_details_new", "board_periods_new", "board_period_refs"):
        conn.execute(f"DELETE FROM temp.{t}")
    periods = _PeriodStore(conn)
    etf_tickers = _etf_tickers(conn)
    for i in range(0, len(ciks), 200):
        hist, unknown = _histories(conn, ciks[i:i + 200], etf_tickers)
        valuations: dict = {}                       # shared by the three windows of this chunk
        have = {h.cik for h in hist}
        hist += [FilerHistory(c, famous_names.get(c, c), {}, {}) for c in ciks[i:i + 200]
                 if c in famous and c not in have]
        unknown_amend |= set(unknown)
        for k, w in windows.items():
            r, u, iss = investors_board(hist, famous, book, w, spy[k], today, reported, valuations, shares)
            _stage_details(conn, "investors", k, r, periods)   # per chunk: details never pile up across filers
            _stage_details(conn, "investors", k, u, periods)
            inv[k][0].extend(r)
            inv[k][1].extend(u)
            x2_issues += iss
    as_of = now.isoformat(timespec="seconds")
    for k, w in windows.items():
        series = benchmark_series(book, w)
        boards[("managers", k)] = assemble("managers", w, *managers_board(managers, book, w, spy[k]), spy[k], as_of, series)
        boards[("investors", k)] = assemble("investors", w, inv[k][0], inv[k][1], spy[k], as_of, series)
        boards[("congress", k)] = assemble("congress", w, *congress_board(members, ctrades, book, w, spy[k]),
                                           spy[k], as_of, series)
        for g in ("managers", "congress"):                # ranked rows carry their rank, as they did in the board
            _stage_details(conn, g, k, boards[(g, k)]["ranked"])
            _stage_details(conn, g, k, boards[(g, k)]["unranked"])
    new_periods = conn.execute("SELECT COUNT(*) FROM temp.board_periods_new").fetchone()[0]
    with db.transaction(conn):
        version = db.bump_version(conn)
        conn.execute("INSERT OR IGNORE INTO board_periods(h, json) SELECT h, json FROM temp.board_periods_new")
        for (g, k), b in boards.items():                  # boards hold no details any more (see _stage_details)
            conn.execute("INSERT OR REPLACE INTO leaderboards(grp, win, json, computed_at, data_version) VALUES(?,?,?,?,?)",
                         (g, k, json.dumps(b, **_JSON), as_of, version))
            conn.execute("DELETE FROM board_details WHERE grp=? AND win=?", (g, k))
        conn.execute("INSERT INTO board_details(grp, win, id, json, data_version) "
                     "SELECT grp, win, id, json, ? FROM temp.board_details_new", (version,))
        conn.execute("DELETE FROM board_periods WHERE h NOT IN (SELECT h FROM temp.board_period_refs)")
        if not live:                              # these depend on stored data only, not on live prices
            conn.execute("UPDATE issues SET resolved=1 WHERE source IN ('leaderboard', 'curated')")
            for kind, ref, msg in x2_issues:
                issues.record(conn, "leaderboard", kind, ref, msg)
            for kind, ref, msg, url in problems:
                issues.record(conn, "leaderboard", kind, ref, msg, url or "")
            for acc in unknown_amend:
                issues.record(conn, "leaderboard", "unknown-amendment-type", acc, "13F-HR/A without RESTATEMENT or NEW HOLDINGS; ignored")
            for m in managers:
                if is_stale(m.verified, today):
                    issues.record(conn, "curated", "stale-entry", f"{m.person}:{m.ticker}", f"verified {m.verified}, over 12 months ago")
    for t in ("board_details_new", "board_periods_new", "board_period_refs"):
        conn.execute(f"DELETE FROM temp.{t}")
    return {"version": version, "investors_ranked": len(inv["1y"][0]), "new_period_blobs": new_periods}


# ---------- first run ----------

SPY_FULL_MIN_ROWS = 2000          # SPY has ~3,700 sessions since 2012; fewer means only recent rows were stored


def _backfill_fund_managers(ctx: Ctx) -> dict:
    etfs = ensure_securities(ctx, {r["ticker"] for r in json.loads(MANAGERS_PATH.read_text())} | {"SPY"})
    n = ctx.conn.execute("SELECT COUNT(*) FROM prices_daily WHERE ticker='SPY'").fetchone()[0]
    if n < SPY_FULL_MIN_ROWS:          # a fresh securities row skips the chart fetch; SPY defines the trading days
        _store_chart(ctx, ctx.yahoo.history("SPY"))
    return {"etfs": sum(etfs.values())}


def backfill(ctx: Ctx) -> dict:
    today = ctx.now().date()
    steps = [("reference", "reference", refresh_reference),
             ("house", "house", lambda c: sync_house(c, list(range(FIRST_YEAR, today.year + 1)))),
             ("senate", "senate", sync_senate),
             ("congress tickers", "reference", resolve_congress_tickers),
             ("13F data sets", "sec13f", sync_13f_datasets),
             ("13F new filings", "sec13f", sync_13f_new),
             ("fund managers", "prices", _backfill_fund_managers),
             ("prices", "prices", lambda c: sync_prices(c, full=False)),   # ensure_securities already stored full charts
             ("leaderboards", "boards", build_boards)]
    failed = []
    for n, (name, source, fn) in enumerate(steps, 1):
        ctx.progress["backfill"] = {"step": n, "of": len(steps), "phase": name}
        log.info("backfill %d/%d: %s", n, len(steps), name)
        try:
            fn(ctx)
        except Exception as e:      # one failing step must not stop the others; its scheduled job takes over
            log.exception("backfill step %s failed", name)
            failed.append(name)
            error = f"{type(e).__name__}: {e}"
            try:
                with db.transaction(ctx.conn):
                    db.set_source_status(ctx.conn, source, False, error)
                    issues.record(ctx.conn, "backfill", "backfill-step-failed", name,
                                  f"first-run step '{name}' failed ({error}); its regular job will fill it in")
                    if name == "house":    # the hourly job reads only two years unless told to catch up
                        db.set_meta(ctx.conn, "house-catch-up", "1")
            except Exception:
                log.exception("could not record failure of backfill step %s", name)
        else:
            with db.transaction(ctx.conn):
                issues.clear_ref(ctx.conn, "backfill", name)
    with db.transaction(ctx.conn):
        db.set_meta(ctx.conn, "backfill_done", ctx.now().isoformat())
    ctx.progress.pop("backfill", None)
    return {"steps": len(steps), "failed": len(failed)}
