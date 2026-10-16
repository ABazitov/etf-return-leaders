"""Group rules (spec §8) turning stored data into leaderboard rows. Pure: data in, dicts out."""
from __future__ import annotations

import bisect
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.congress_sim import Amount, SimTrade, simulate
from app.curated import ManagerFund
from app.returns import (  # noqa: F401  (DenseSeries is re-exported for callers)
    DenseSeries, MissingPrice, annualize, last_trading_day_on_or_before, pp_vs, rank_rows, simple_return, twr, window_start,
)

WINDOWS = {"1y": 1, "3y": 3, "5y": 5}
LISTING_MIN_USD = 10_000_000      # F8
UNPRICED_MAX = 0.05               # R11
INVESTED_MIN = 0.95               # C7
X2_TOLERANCE = 0.10               # X2
# X2 accepts a price only if |reported - shares x close| <= 10% of shares x close, so a priced position is worth at
# most reported / 0.9. A filer whose reported ETF total is below $9M at a period can therefore never reach the $10M
# F8 listing value there; the tiny margin absorbs floating-point rounding in that bound.
REPORTED_FLOOR_USD = LISTING_MIN_USD * (1 - X2_TOLERANCE) * (1 - 1e-9)


@dataclass
class PriceBook:
    tri: dict[str, Mapping]
    close: dict[str, Mapping]
    trading_days: list[date]
    bad: set[str] = field(default_factory=set)
    splits: dict[str, list[tuple[date, float]]] = field(default_factory=dict)   # (effective day, ratio), by day

    def has(self, ticker: str | None, day: date) -> bool:
        return bool(ticker) and ticker not in self.bad and day in self.tri.get(ticker, {})

    def has_all(self, ticker: str | None, days: list[date]) -> bool:
        """`bool(ticker) and all(self.has(ticker, d) for d in days)`, reading the array directly when it can."""
        if not ticker:
            return False
        if not days:
            return True
        if ticker in self.bad:
            return False
        s = self.tri.get(ticker)
        if s is None:
            return False
        if isinstance(s, DenseSeries):
            idx, v = s._idx, s._v
            n = len(v)
            for d in days:
                i = idx.get(d)
                if i is None or i >= n or v[i] != v[i]:
                    return False
            return True
        return all(d in s for d in days)

    def close_on(self, ticker: str | None, day: date) -> float | None:
        s = self.close.get(ticker)
        if s is None:
            return None
        try:
            return s[day]
        except KeyError:
            return None

    def split_factor_after(self, ticker: str | None, day: date) -> float:
        """New shares per share held on `day`, from the splits effective after it (3.0 after one 3-for-1 split).
        Closes are split-adjusted, so the price as quoted on `day` is close_on(day) times this."""
        f = 1.0
        for d, ratio in self.splits.get(ticker, ()):
            if d > day:
                f *= ratio
        return f

    def covers(self, ticker: str, start: date, end: date) -> bool:
        return self.has(ticker, start) and self.has(ticker, end)


@dataclass(frozen=True)
class Window:
    key: str
    years: int
    start: date
    end: date


def make_window(key: str, book: PriceBook, today: date) -> Window:
    years = WINDOWS[key]
    return Window(key, years, window_start(today, years, book.trading_days), book.trading_days[-1])


def _figures(total: float, w: Window, spy_total: float) -> dict:
    return {"total": total, "annualized": annualize(total, w.start, w.end) if w.years > 1 else None,
            "vs_spx": pp_vs(total, spy_total)}


def _weekly(points: list[tuple[date, float]]) -> list[list]:
    if not points:
        return []
    keep = [p for i, p in enumerate(points) if i % 5 == 0]
    if keep[-1] is not points[-1]:
        keep.append(points[-1])
    return [[d.isoformat(), round(g, 6)] for d, g in keep]


def _points(points) -> list[list]:
    return [[d.isoformat(), round(g, 6)] for d, g in points]


def _missing_reason(e: MissingPrice) -> str:
    return f"insufficient price data (no price for {e.ticker} on {e.day})"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.casefold()).strip("-")


# ---------- fund managers (M4-M6) ----------

def managers_board(funds: list[ManagerFund], book: PriceBook, w: Window, spy_total: float):
    by_person: dict[str, list[ManagerFund]] = defaultdict(list)
    for f in funds:
        by_person[f.person].append(f)
    ranked, unranked = [], []
    days = [d for d in book.trading_days if w.start <= d <= w.end]
    for person, fs in by_person.items():
        detail, qualifying = [], []
        for f in sorted(fs, key=lambda f: f.ticker):
            reason = ""
            if f.tenure_start > w.start:
                reason = f"managed since {f.since_text}, shorter than the window"
            elif not book.covers(f.ticker, w.start, w.end):
                reason = "no price data for the whole window"
            r = None if reason else simple_return(book.tri[f.ticker], w.start, w.end)
            if r is not None:
                qualifying.append(f.ticker)
            detail.append({"ticker": f.ticker, "fund_name": f.fund_name, "since": f.since_text, "total": r,
                           "counts": r is not None, "reason": reason, "source_url": f.source_url,
                           "evidence": f.evidence, "verified": f.verified.isoformat()})
        row = {"id": _slug(person), "name": person, "badge": "EXACT",
               "funds": [d["ticker"] for d in detail if d["counts"]], "detail": {"funds": detail}}
        if not qualifying:
            row["reason"] = "no fund managed for the whole window"
            unranked.append(row)
            continue
        total = sum(d["total"] for d in detail if d["counts"]) / len(qualifying)
        row.update(_figures(total, w, spy_total))
        pts = []
        for d in days:
            if all(book.has(t, d) for t in qualifying):
                pts.append((d, sum(book.tri[t][d] / book.tri[t][w.start] for t in qualifying) / len(qualifying)))
        row["detail"]["series"] = _weekly(pts)
        ranked.append(row)
    return ranked, unranked


# ---------- 13F investors (F5-F8, R11, X2) ----------

def _qend(y: int, q: int) -> date:
    return {1: date(y, 3, 31), 2: date(y, 6, 30), 3: date(y, 9, 30), 4: date(y, 12, 31)}[q]


def last_quarter_end_on_or_before(d: date) -> date:
    for q in (4, 3, 2, 1):
        if _qend(d.year, q) <= d:
            return _qend(d.year, q)
    return _qend(d.year - 1, 4)


def quarter_ends_between(a: date, b: date) -> list[date]:
    out, y = [], a.year
    while y <= b.year:
        out += [_qend(y, q) for q in (1, 2, 3, 4) if a <= _qend(y, q) <= b]
        y += 1
    return out


def latest_due_quarter(today: date) -> date:
    """Most recent quarter-end whose 45-day filing deadline has passed."""
    q = last_quarter_end_on_or_before(today)
    while not q + timedelta(days=45) < today:
        q = last_quarter_end_on_or_before(q - timedelta(days=1))
    return q


@dataclass(frozen=True)
class Position:
    cusip: str
    ticker: str | None
    shares: float
    reported_usd: float
    name: str


@dataclass(frozen=True)
class FilerHistory:
    cik: str
    name: str
    periods: dict[date, list[Position]]
    accessions: dict[date, str]


def _filing_url(cik: str, acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/"


def required_periods(periods, w: Window, today: date) -> list[date]:
    """Quarter-ends a filer must have reported for window `w`: from the one on or before the start through the
    latest one due, plus any newer ones it already filed."""
    due = latest_due_quarter(today)
    return quarter_ends_between(last_quarter_end_on_or_before(w.start), due) + sorted(p for p in periods if p > due)


def cannot_be_listed(reported: dict[date, float], w: Window, today: date) -> bool:
    """True if F8 must fail for window `w`, from per-period reported ETF totals (sums of max(value, 0)) alone."""
    present = [p for p in required_periods(reported, w, today) if p in reported]
    return not present or any(reported[p] < REPORTED_FLOOR_USD for p in present)


def _value_period(h: FilerHistory, p: date, t: date, hold_days: list[date], book: PriceBook,
                  priced: dict[str | None, tuple[float, float] | None]):
    """Value one reported period at its quarter-end close t: X2 check per position, units of TRI held after t.
    `priced` caches, per ticker for these holding days, (price at t, TRI at t) if priced throughout, else None. The
    price is as quoted on t: a 13F counts the shares held then, before any later split our adjusted closes include."""
    p_issues: list[tuple[str, str, str]] = []
    n_checked = n_failed = 0
    units: dict[str, float] = defaultdict(float)
    priced_value = unpriced_rep = total_rep = 0.0
    rows = []
    for pos in h.periods[p]:
        total_rep += pos.reported_usd
        mv = None
        if pos.ticker in priced:
            hit = priced[pos.ticker]
        else:
            hit = None
            if book.has_all(pos.ticker, hold_days):
                c = book.close_on(pos.ticker, t)
                hit = None if c is None else (c * book.split_factor_after(pos.ticker, t),
                                              book.tri[pos.ticker][t])     # priced through t: TRI(t) exists
            priced[pos.ticker] = hit
        c, tri_t = hit if hit is not None else (None, None)
        if c is not None:
            mv = pos.shares * c
            n_checked += 1
            if mv <= 0 or abs(pos.reported_usd - mv) / mv > X2_TOLERANCE:
                n_failed += 1
                p_issues.append(("x2-mismatch", f"{h.cik}:{p}:{pos.cusip}",
                                 f"{h.name} {p}: reported ${pos.reported_usd:,.0f} but {pos.shares:,.0f} shares "
                                 f"of {pos.ticker} at ${c:,.2f} = ${mv:,.0f}"))
                mv = None
        if mv is None:
            unpriced_rep += pos.reported_usd
        else:
            units[pos.ticker] += pos.shares * c / tri_t
            priced_value += mv
        rows.append({"ticker": pos.ticker, "cusip": pos.cusip, "name": pos.name, "shares": pos.shares,
                     "reported_usd": pos.reported_usd, "value_usd": mv, "priced": mv is not None})
    return n_checked, n_failed, units, priced_value, unpriced_rep, total_rep, rows, p_issues


def investors_board(histories: list[FilerHistory], famous: dict[str, tuple[str, str]], book: PriceBook,
                    w: Window, spy_total: float, today: date,
                    reported: Mapping[str, dict[date, float]] | None = None, valuations: dict | None = None,
                    etf_share: Mapping[str, Mapping[date, float | None]] | None = None):
    """`reported` (optional) gives per filer and period an upper bound on the reported ETF total, to skip filers
    that cannot be listed; without it the bound is summed from the positions. `valuations` (optional) is a cache
    shared by calls for other windows over the same histories and book: a period valued over the same holding days
    gives the same result in every window. `etf_share` (optional) gives per filer and period the reported ETF value
    as a share of the reported book (None if unknown): shown for the latest period and averaged over the window."""
    valuations = valuations if valuations is not None else {}
    ranked, unranked, issues = [], [], []
    weekly_days = [d for i, d in enumerate(book.trading_days) if w.start <= d <= w.end and i % 5 == 0]
    priced_cache: dict[tuple[date, ...], dict[str | None, tuple[float, float] | None]] = {}
    for h in histories:
        required = required_periods(h.periods, w, today)
        present = [p for p in required if p in h.periods]
        is_famous = h.cik in famous
        bound = reported.get(h.cik, {}) if reported is not None else \
            {p: sum([max(pos.reported_usd, 0.0) for pos in h.periods[p]]) for p in present}
        if not is_famous and cannot_be_listed({p: bound.get(p, float("inf")) for p in present}, w, today):
            continue                    # would be dropped below as not listed; skip the valuation work
        values, gate, unpriced_share, units_at, periods_detail = {}, {}, {}, {}, []
        shares = (etf_share or {}).get(h.cik, {})
        h_issues: list[tuple[str, str, str]] = []
        t_of = {p: last_trading_day_on_or_before(p, book.trading_days) for p in present}
        reduced = sorted({w.start, w.end, *weekly_days, *(t for t in t_of.values() if w.start < t <= w.end)})
        for n, p in enumerate(present):
            t = t_of[p]
            lo = max(t, w.start)
            hi = min(t_of[present[n + 1]], w.end) if n + 1 < len(present) else w.end
            hold_days = reduced[bisect.bisect_left(reduced, lo):bisect.bisect_right(reduced, hi)]
            if not hold_days or hold_days[0] != t:      # t <= lo: the same list as sorted({t, *reduced in [lo, hi]})
                hold_days = [t, *hold_days]
            key = tuple(hold_days)
            hit = valuations.get((h.cik, p, key))
            if hit is None:
                hit = valuations[(h.cik, p, key)] = _value_period(h, p, t, hold_days, book,
                                                                   priced_cache.setdefault(key, {}))
            n_checked, n_failed, units, priced_value, unpriced_rep, total_rep, rows, p_issues = hit
            h_issues.extend(p_issues)
            if n_checked and n_failed / n_checked > 0.5:
                h_issues.append(("x2-likely-unit-error", f"{h.cik}:{p}",
                                 f"{h.name} {p}: {n_failed} of {n_checked} priced positions disagree with "
                                 f"reported values by more than 10%; the filing may use the wrong units"))
            values[p] = priced_value
            gate[p] = priced_value + unpriced_rep       # F8 listing value: unpriceable ETFs at reported value
            unpriced_share[p] = unpriced_rep / total_rep if total_rep else 0.0
            units_at[p] = (t, dict(units))
            periods_detail.append({"period": p.isoformat(), "etf_value_usd": priced_value,
                                   "etf_share": shares.get(p), "url": _filing_url(h.cik, h.accessions[p]),
                                   "positions": rows})
        listed = bool(present) and all(gate[p] >= LISTING_MIN_USD for p in present)
        if not listed and not is_famous:
            continue
        issues.extend(h_issues)
        person, role = famous.get(h.cik, ("", ""))
        window_shares = [shares.get(p) for p in present]
        row = {"id": h.cik, "name": h.name, "person": person, "role": role, "badge": "APPROX",
               "etf_share": window_shares[-1] if window_shares else None,
               "etf_share_avg": (sum(window_shares) / len(window_shares)
                                 if window_shares and None not in window_shares else None),
               "detail": {"periods": periods_detail}}
        missing = [p for p in required if p not in h.periods]
        reason = ""
        if missing:
            reason = f"missing report for {missing[0].isoformat()}"
        elif not listed:
            low = next(p for p in present if gate[p] < LISTING_MIN_USD)
            reason = f"ETF holdings below $10M at {low.isoformat()}"
        elif any(unpriced_share[p] > UNPRICED_MAX for p in present):
            reason = "insufficient price data"
        if reason:
            row["reason"] = reason
            unranked.append(row)
            continue
        changes, held = [], set()
        for p in present:
            t, units = units_at[p]
            for k in held - set(units):
                changes.append((t, k, 0.0))
            changes += [(t, k, u) for k, u in units.items()]
            held = set(units)
        # Units only change on rebalance days, so chaining over a reduced day list is exact (buy-and-hold telescopes).
        try:
            res = twr(changes, book.tri, reduced, w.start, w.end, with_series=True)
        except MissingPrice as e:
            row["reason"] = _missing_reason(e)
            unranked.append(row)
            continue
        if res.total is None:
            row["reason"] = "no ETF holdings during the window"
            unranked.append(row)
            continue
        latest_p = present[-1]
        t_last, last_units = units_at[latest_p]
        by_value = sorted(last_units.items(), key=lambda kv: -kv[1] * book.tri[kv[0]][t_last])
        row.update(_figures(res.total, w, spy_total))
        row.update({"etf_value_usd": values[latest_p], "n_etfs": len(last_units),
                    "top_etf": by_value[0][0] if by_value else None, "latest_period": latest_p.isoformat()})
        row["detail"]["series"] = _points(res.series)
        ranked.append(row)
    return ranked, unranked, issues


# ---------- Congress (C4-C7, R11) ----------

@dataclass(frozen=True)
class CTrade:
    report_id: str
    row: int
    filed: date
    tx_date: date
    ticker: str
    kind: str
    amount: Amount
    amount_text: str
    url: str


@dataclass(frozen=True)
class MemberInfo:
    key: str
    name: str
    chamber: str
    party: str
    state: str
    service: tuple[tuple[date, date], ...] = ()     # C11 times in office; empty for name-only members (unknown)


def service_rule(service: tuple[tuple[date, date], ...], w: Window) -> tuple[bool, date | None, str | None]:
    """C11: (listed, last day of earlier service whose trades no longer count, reason not ranked). Trades are reported
    only while in office, so a member's holdings end when they leave. Unknown service sets no limit."""
    if not service:
        return True, None, None
    end = service[-1][1]
    if end < w.start:
        return False, None, None
    if end < w.end:
        return True, None, f"left office on {end}; trades after that are not reported"
    breaks = [(e, s) for (_, e), (s, _) in zip(service, service[1:]) if e >= w.start]
    if breaks:
        return True, None, f"out of office from {breaks[-1][0]} to {breaks[-1][1]}; trades in that time are not reported"
    return True, (service[-2][1] if len(service) > 1 else None), None


def congress_board(members: dict[str, MemberInfo], trades: dict[str, list[CTrade]], book: PriceBook,
                   w: Window, spy_total: float):
    ranked, unranked = [], []
    for key, ts in trades.items():
        listed, cut, not_ranked = service_rule(members[key].service, w)
        if not listed:
            continue
        n_earlier = sum(1 for t in ts if cut and t.tx_date <= cut)
        ts = sorted((t for t in ts if t.tx_date <= w.end and not (cut and t.tx_date <= cut)),
                    key=lambda t: (t.tx_date, t.filed, t.row))
        full_sales: dict[str, list[date]] = defaultdict(list)
        for t in ts:
            if t.kind == "sell_full":
                full_sales[t.ticker].append(t.tx_date)
        priced: list[SimTrade] = []
        unpriced_refs, detail_trades = set(), []
        by_ticker: dict[str, list[CTrade]] = defaultdict(list)
        for t in ts:
            by_ticker[t.ticker].append(t)
        bad_tickers = set()
        for tk, tts in by_ticker.items():
            need: set[date] = set()
            start_d = None
            for t in tts:
                if t.tx_date >= book.trading_days[0]:
                    need.add(last_trading_day_on_or_before(t.tx_date, book.trading_days))
                if start_d is None and t.kind == "buy":
                    start_d = t.tx_date
                elif start_d is not None and t.kind == "sell_full":
                    need.update(d for d in book.trading_days if max(start_d, w.start) <= d <= min(t.tx_date, w.end))
                    start_d = None
            if start_d is not None:
                need.update(d for d in book.trading_days if max(start_d, w.start) <= d <= w.end)
            if not book.has_all(tk, sorted(need)):
                bad_tickers.add(tk)
        for t in ts:
            ref = f"{t.report_id}#{t.row}"
            day = None
            if t.tx_date >= book.trading_days[0]:
                day = last_trading_day_on_or_before(t.tx_date, book.trading_days)
            ok = day is not None and t.ticker not in bad_tickers and book.has(t.ticker, day)
            if ok:
                priced.append(SimTrade(day, t.ticker, t.kind, t.amount, ref))
            else:
                unpriced_refs.add(ref)
            detail_trades.append({"ref": ref, "ticker": t.ticker, "kind": t.kind, "tx_date": t.tx_date.isoformat(),
                                  "amount": t.amount_text, "open_ended": t.amount.open_ended, "priced": ok,
                                  "url": t.url})

        def counts(t: CTrade) -> bool:
            return t.kind == "buy" and (t.tx_date > w.start or not any(t.tx_date <= s <= w.start
                                                                       for s in full_sales[t.ticker]))

        counted = [t for t in ts if counts(t)]
        total_mid = sum(t.amount.mid for t in counted)
        if total_mid == 0:
            continue
        unpriced_mid = sum(t.amount.mid for t in counted if f"{t.report_id}#{t.row}" in unpriced_refs)
        m = members[key]
        row = {"id": key, "name": m.name, "chamber": m.chamber, "party_state": f"{m.party}-{m.state}",
               "badge": "EST", "n_trades": len(ts), "detail": {"trades": detail_trades, "notes": []}}
        if n_earlier:
            row["detail"]["notes"].append({"ref": f"until {cut}", "message": f"{n_earlier} trade{'s' * (n_earlier > 1)} "
                                           "from an earlier time in office not counted: holdings end when a member "
                                           "leaves office"})
        if not_ranked:
            row["reason"] = not_ranked
            unranked.append(row)
            continue
        if unpriced_mid / total_mid > UNPRICED_MAX:
            row["reason"] = "insufficient price data"
            unranked.append(row)
            continue
        results, final_units = {}, {}
        try:
            for which in ("mid", "low", "high"):
                changes, notes = simulate(priced, book.tri, which)
                results[which] = twr(changes, book.tri, book.trading_days, w.start, w.end,
                                     with_series=(which == "mid"))
                if which == "mid":
                    row["detail"]["notes"] += [{"ref": r, "message": msg} for r, msg in notes]
                    for d, k, u in changes:
                        final_units[k] = u
        except MissingPrice as e:
            row["reason"] = _missing_reason(e)
            unranked.append(row)
            continue
        mid = results["mid"]
        if mid.invested_days == 0:
            continue
        row.update(_figures(mid.total, w, spy_total))
        row["low"], row["high"] = results["low"].total, results["high"].total
        held = {k: u * book.tri[k][w.end] for k, u in final_units.items() if u > 0 and book.has(k, w.end)}
        row["top_etf"] = max(held, key=held.get) if held else None
        row["invested_fraction"] = mid.invested_fraction
        row["detail"]["series"] = _weekly(list(mid.series))
        if mid.invested_fraction < INVESTED_MIN:
            row["reason"] = f"invested on {mid.invested_fraction * 100:.1f}% of trading days"
            unranked.append(row)
        else:
            ranked.append(row)
    return ranked, unranked


# ---------- board assembly ----------

def benchmark_series(book: PriceBook, w: Window, ticker: str = "SPY") -> list[list]:
    pts = [(d, book.tri[ticker][d] / book.tri[ticker][w.start])
           for d in book.trading_days if w.start <= d <= w.end and book.has(ticker, d)]
    return _weekly(pts)


def assemble(group: str, w: Window, ranked: list[dict], unranked: list[dict], spy_total: float, as_of: str,
             spy_series: list | None = None) -> dict:
    bench = {"ticker": "SPY", "total": spy_total}
    if spy_series is not None:
        bench["series"] = spy_series
    return {"group": group, "window": w.key, "years": w.years, "start": w.start.isoformat(), "end": w.end.isoformat(),
            "benchmark": bench, "as_of": as_of,
            "ranked": rank_rows(ranked), "unranked": sorted(unranked, key=lambda r: r["name"].casefold())}


def strip_detail(board: dict) -> dict:
    out = dict(board)
    out["ranked"] = [{k: v for k, v in r.items() if k != "detail"} for r in board["ranked"]]
    out["unranked"] = [{k: v for k, v in r.items() if k != "detail"} for r in board["unranked"]]
    return out
