"""Congress trade sizes (spec C3) and the holdings simulation (C5, C6) feeding the TWR engine."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.returns import MissingPrice, UnitChange

_RANGE = re.compile(r"^\$([\d,]+)\s*-\s*\$([\d,]+)$")
_OVER = re.compile(r"^(?:Spouse/DC\s+)?Over\s+\$([\d,]+)$")
_EXACT = re.compile(r"^\$([\d,]+(?:\.\d{1,2})?)$")

TX_KIND: dict[str, str | None] = {
    "P": "buy", "Purchase": "buy",
    "S": "sell_full", "Sale (Full)": "sell_full",
    "S (partial)": "sell_partial", "Sale (Partial)": "sell_partial",
    "E": None, "Exchange": None,
}


@dataclass(frozen=True)
class Amount:
    low: float
    high: float
    open_ended: bool
    exact: bool

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def pick(self, which: str) -> float:
        return {"low": self.low, "mid": self.mid, "high": self.high}[which]


def _num(s: str) -> float:
    v = float(s.replace(",", ""))
    return int(v) if v.is_integer() else v


def parse_amount(text: str) -> Amount:
    t = re.sub(r"\s+", " ", text).strip()
    if m := _RANGE.match(t):
        return Amount(_num(m.group(1)), _num(m.group(2)), False, False)
    if m := _OVER.match(t):
        v = _num(m.group(1)) + 1
        return Amount(v, v, True, False)
    if m := _EXACT.match(t):
        v = _num(m.group(1))
        return Amount(v, v, False, True)
    raise ValueError(f"unknown amount format: {text!r}")


@dataclass(frozen=True)
class SimTrade:
    day: date          # already mapped to the last trading day on or before the transaction date
    ticker: str
    kind: str          # "buy" | "sell_partial" | "sell_full"
    amount: Amount
    ref: str           # report id + row, for notes and the details panel


def simulate(trades: list[SimTrade], tri_book: dict[str, dict[date, float]],
             which: str = "mid") -> tuple[list[UnitChange], list[tuple[str, str]]]:
    """Apply trades in the given order (caller sorts: tx date, filing date, row). Returns absolute unit changes."""
    units: dict[str, float] = {}
    changes: list[UnitChange] = []
    notes: list[tuple[str, str]] = []
    for t in trades:
        held = units.get(t.ticker, 0.0)
        if t.kind != "buy" and held <= 0:
            notes.append((t.ref, "sale of a position bought before the disclosure record"))
            continue
        try:
            price = tri_book[t.ticker][t.day]
        except KeyError:
            raise MissingPrice(t.ticker, t.day) from None
        if t.kind == "buy":
            new = held + t.amount.pick(which) / price
        elif t.kind == "sell_partial":
            new = held - min(held, t.amount.pick(which) / price)
        elif t.kind == "sell_full":
            new = 0.0
        else:
            raise ValueError(f"unknown trade kind {t.kind!r}")
        units[t.ticker] = new
        changes.append((t.day, t.ticker, new))
    return changes, notes
