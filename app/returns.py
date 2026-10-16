"""Pure return math (spec §7). No I/O, no clock: callers pass `today` and data in."""
from __future__ import annotations

import bisect
import math
from array import array
from collections.abc import Mapping
from dataclasses import dataclass
from operator import itemgetter, mul
from datetime import date
from typing import Iterable

UnitChange = tuple[date, str, float]


class MissingPrice(Exception):
    def __init__(self, ticker: str, day: date):
        super().__init__(f"no price for {ticker} on {day}")
        self.ticker = ticker
        self.day = day


class DenseSeries(Mapping):
    """date -> float backed by a shared day index and a compact array; NaN means no value that day."""

    def __init__(self, days_index: dict[date, int], values: array):
        self._idx, self._v = days_index, values

    def __getitem__(self, day: date) -> float:
        i = self._idx.get(day)
        if i is not None and i < len(self._v):
            v = self._v[i]
            if v == v:                      # not NaN
                return v
        raise KeyError(day)

    def __contains__(self, day) -> bool:
        i = self._idx.get(day)
        return i is not None and i < len(self._v) and self._v[i] == self._v[i]

    def __iter__(self):
        return (d for d, i in self._idx.items() if i < len(self._v) and not math.isnan(self._v[i]))

    def __len__(self) -> int:
        return sum(1 for _ in self)


def build_tri(rows: Iterable[tuple[date, float, float]]) -> dict[date, float]:
    """R2: TRI(first)=1; TRI(d) = TRI(prev) * (c(d) + D(d)) / c(prev)."""
    tri: dict[date, float] = {}
    prev_close: float | None = None
    value = 1.0
    for day, close, dividend in rows:
        if close is None or close <= 0:
            raise ValueError(f"bad close {close!r} on {day}")
        if prev_close is not None:
            value = value * (close + dividend) / prev_close
        tri[day] = value
        prev_close = close
    return tri


def last_trading_day_on_or_before(day: date, trading_days: list[date]) -> date:
    i = bisect.bisect_right(trading_days, day)
    if i == 0:
        raise ValueError(f"no trading day on or before {day}")
    return trading_days[i - 1]


def window_start(today: date, years: int, trading_days: list[date]) -> date:
    """R4: last trading day on or before the date `years` calendar years before today (Feb 29 -> Feb 28)."""
    try:
        target = today.replace(year=today.year - years)
    except ValueError:
        target = today.replace(year=today.year - years, day=28)
    return last_trading_day_on_or_before(target, trading_days)


def _px(tri_book: dict[str, dict[date, float]], ticker: str, day: date) -> float:
    try:
        return tri_book[ticker][day]
    except KeyError:
        raise MissingPrice(ticker, day) from None


def simple_return(tri: dict[date, float], start: date, end: date) -> float:
    """R5."""
    for d in (start, end):
        if d not in tri:
            raise MissingPrice("?", d)
    return tri[end] / tri[start] - 1


@dataclass(frozen=True)
class TwrResult:
    total: float | None
    invested_days: int
    total_days: int
    series: tuple = ()          # ((day, growth of $1), ...) when requested; used for the details-panel chart

    @property
    def invested_fraction(self) -> float:
        return self.invested_days / self.total_days if self.total_days else 0.0


def _is_trading_day(day: date, trading_days: list[date]) -> bool:
    i = bisect.bisect_left(trading_days, day)
    return i < len(trading_days) and trading_days[i] == day


class _Holdings:
    """Units held (u > 0) and a fast valuation. Values are summed in the same order with the same products as
    sum(u * tri[k][day] for k, u in held.items()), so results are bit-for-bit those of the plain loop."""

    def __init__(self, units: dict[str, float], tri_book):
        self.held = {k: u for k, u in units.items() if u > 0}
        self.tri_book = tri_book
        self.dense = None
        series = [tri_book.get(k) for k in self.held]
        if series and all(isinstance(s, DenseSeries) for s in series) and \
                all(s._idx is series[0]._idx for s in series):
            self.idx = series[0]._idx
            self.dense = True
            self.units = list(self.held.values())
            self.arrays = [s._v for s in series]
            self.n = min(len(a) for a in self.arrays)

    def value(self, day: date) -> float:
        if self.dense is not None:
            i = self.idx.get(day)
            if i is not None and i < self.n:
                v = sum(map(mul, self.units, map(itemgetter(i), self.arrays)))     # u * tri, in held order
                if v == v:                  # no NaN, i.e. every price present
                    return v
        return sum(u * _px(self.tri_book, k, day) for k, u in self.held.items())    # raises MissingPrice


def twr(changes: list[UnitChange], tri_book: dict[str, dict[date, float]],
        trading_days: list[date], start: date, end: date, with_series: bool = False) -> TwrResult:
    """R6/R7: chain daily returns over trading days in (start, end] using units held before each day's changes."""
    days = trading_days[bisect.bisect_right(trading_days, start):bisect.bisect_right(trading_days, end)]
    pending = sorted(changes, key=lambda c: c[0])  # stable: keeps caller order within a day
    units: dict[str, float] = {}
    i = 0
    while i < len(pending) and pending[i][0] <= start:
        units[pending[i][1]] = pending[i][2]
        i += 1
    growth, invested, prev = 1.0, 0, start
    series = [(start, 1.0)]
    holdings = _Holdings(units, tri_book)
    v_carry = None              # value of the current holdings at `prev`, known when units did not change since
    for t in days:
        if holdings.held:
            v_prev = v_carry if v_carry is not None else holdings.value(prev)
            v_now = holdings.value(t)
            growth *= v_now / v_prev
            invested += 1
            v_carry = v_now
        changed, checked = False, None
        while i < len(pending) and pending[i][0] <= t:
            day, ticker, u = pending[i]
            if day != checked and not _is_trading_day(day, trading_days):
                raise ValueError(f"unit change on non-trading day {day} inside the window")
            checked = day
            units[ticker] = u
            changed = True
            i += 1
        if changed:
            holdings, v_carry = _Holdings(units, tri_book), None
        prev = t
        series.append((t, growth))
    return TwrResult(total=(growth - 1) if invested else None, invested_days=invested, total_days=len(days),
                     series=tuple(series) if with_series else ())


def annualize(total: float, start: date, end: date) -> float:
    """R8."""
    days = (end - start).days
    return (1 + total) ** (365.25 / days) - 1


def pp_vs(total: float, benchmark: float) -> float:
    """R9: difference in percentage points."""
    return (total - benchmark) * 100


def rank_rows(rows: list[dict]) -> list[dict]:
    """R10: total desc, ties alphabetical by name (case-insensitive)."""
    ordered = sorted(rows, key=lambda r: (-r["total"], r["name"].casefold()))
    for n, r in enumerate(ordered, start=1):
        r["rank"] = n
    return ordered
