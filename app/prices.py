"""Yahoo Finance prices (unofficial API) + checks X1 and X5. Only this module knows Yahoo's formats."""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app.config import YAHOO_USER_AGENT
from app.http import HttpError, fetch
from app.returns import build_tri

NY = ZoneInfo("America/New_York")
log = logging.getLogger("etf.prices")
_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?period1=0&period2=9999999999&interval=1d&events=div%2Csplit&includeAdjustedClose=true"
_CHART_RECENT = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=5d&interval=1d&events=div%2Csplit&includeAdjustedClose=true"
_SPARK = "https://query1.finance.yahoo.com/v8/finance/spark?symbols={s}&range=1d&interval=5m"
_SESSION_DONE = time(16, 30)


@dataclass(frozen=True)
class DailyRow:
    day: date
    close: float
    dividend: float
    split: float
    adjclose: float | None


@dataclass(frozen=True)
class Chart:
    ticker: str
    instrument_type: str
    rows: list[DailyRow]
    market_time: datetime | None
    dropped_bad_closes: int = 0


def _ny_date(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), NY).date()


def parse_chart(payload: dict) -> Chart:
    res = payload["chart"]["result"][0]
    meta = res["meta"]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or [None] * len(ts)
    events = res.get("events") or {}
    divs: dict[date, float] = {}
    for v in (events.get("dividends") or {}).values():
        d = _ny_date(v["date"])
        divs[d] = divs.get(d, 0.0) + float(v["amount"])
    splits = {_ny_date(v["date"]): float(v["numerator"]) / float(v["denominator"])
              for v in (events.get("splits") or {}).values()}
    good = [(t, c, a) for t, c, a in zip(ts, closes, adj) if c is not None]
    good = [(t, c, a) for t, c, a in good if math.isfinite(float(c)) and float(c) > 0]    # zero/negative/NaN/inf closes
    dropped = sum(1 for c in closes if c is not None) - len(good)
    rows = [DailyRow(_ny_date(t), float(c), divs.get(_ny_date(t), 0.0), splits.get(_ny_date(t), 1.0),
                     float(a) if a is not None else None)
            for t, c, a in good]
    mt = meta.get("regularMarketTime")
    return Chart(meta["symbol"], meta.get("instrumentType", ""), rows,
                 datetime.fromtimestamp(mt, NY) if mt else None, dropped)


def completed_rows(rows: list[DailyRow], now_et: datetime) -> list[DailyRow]:
    today = now_et.date()
    done_today = now_et.time() >= _SESSION_DONE
    return [r for r in rows if r.day < today or (r.day == today and done_today)]


def parse_spark(payload: dict) -> dict[str, tuple[float, datetime]]:
    out = {}
    for sym, v in payload.items():
        pairs = [(t, c) for t, c in zip(v.get("timestamp") or [], v.get("close") or []) if c is not None and math.isfinite(float(c)) and float(c) > 0]
        if pairs:
            t, c = pairs[-1]
            out[sym] = (float(c), datetime.fromtimestamp(t, NY))
    return out


def x1_check(rows: list[DailyRow], since: date = date(2012, 1, 1),
             tolerance: float = 0.005) -> tuple[bool, float, date | None]:
    """X1: cumulative TRI vs Yahoo adjusted close at every month-end since `since`."""
    rows = [r for r in rows if r.adjclose]
    if not rows:
        return False, float("inf"), None
    tri = build_tri((r.day, r.close, r.dividend) for r in rows)
    span = [r for r in rows if r.day >= since]
    if not span:
        return True, 0.0, None
    base = span[0]
    worst, worst_day = 0.0, None
    for i, r in enumerate(span):
        is_month_end = i + 1 == len(span) or span[i + 1].day.month != r.day.month
        if not is_month_end:
            continue
        ours = tri[r.day] / tri[base.day]
        theirs = r.adjclose / base.adjclose
        dev = abs(ours / theirs - 1)
        if dev > worst:
            worst, worst_day = dev, r.day
    return worst <= tolerance, worst, worst_day


def x5_flags(rows: list[DailyRow]) -> list[date]:
    """X5: close-to-close move above 50% on a day with no split."""
    return [b.day for a, b in zip(rows, rows[1:]) if a.close > 0 and b.split == 1.0 and abs(b.close / a.close - 1) > 0.5]


class YahooProvider:
    def history(self, ticker: str, recent: bool = False) -> Chart:
        url = (_CHART_RECENT if recent else _CHART).format(t=quote(ticker))
        return parse_chart(json.loads(fetch(url, ua=YAHOO_USER_AGENT, min_interval=0.5)))

    def latest(self, tickers: list[str]) -> dict[str, tuple[float, datetime]]:
        out: dict[str, tuple[float, datetime]] = {}
        for i in range(0, len(tickers), 20):
            batch = ",".join(quote(t) for t in tickers[i:i + 20])
            try:
                body = fetch(_SPARK.format(s=batch), ua=YAHOO_USER_AGENT, min_interval=0.5)
            except HttpError as e:
                if e.status == 404:          # Yahoo answers 404 only when no symbol in the batch is valid
                    log.warning("spark: no valid symbol in %s; skipped", batch)
                    continue
                log.error("spark request failed for %s: %s", batch, e)
                raise
            out.update(parse_spark(json.loads(body)))
        return out


X6_WINDOW_YEARS = 5
X6_MAX_COUNT = 2
X6_MAX_YIELD = 0.05


def apply_dividend_corrections(rows: list[DailyRow], corrections) -> tuple[list[DailyRow], list]:
    """Zero (remove) or overwrite (set) the dividend on the matching day. Returns (new rows, corrections with no bar)."""
    by_day = {c.ex_date: c for c in corrections}
    seen, out = set(), []
    for r in rows:
        c = by_day.get(r.day)
        if c is None:
            out.append(r)
            continue
        seen.add(r.day)
        out.append(replace(r, dividend=0.0 if c.action == "remove" else c.amount))
    return out, [c for c in corrections if c.ex_date not in seen]


def x6_irregular_dividends(rows: list[DailyRow]) -> list[date]:
    """X6: every dividend (in the recent window) of a ticker with at most 2 dividends there, plus any above 5% of the prior close."""
    if not rows:
        return []
    start = rows[-1].day - timedelta(days=round(365.25 * X6_WINDOW_YEARS))
    sparse = sum(1 for r in rows if r.dividend and r.day > start) <= X6_MAX_COUNT
    return [b.day for a, b in zip([None] + rows[:-1], rows)
            if b.dividend and ((sparse and b.day > start) or (a is not None and b.dividend > X6_MAX_YIELD * a.close))]
