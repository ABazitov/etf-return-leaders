"""Hand-checked lists (spec M1-M3, F9). Every entry must carry its source and verification date."""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent / "curated"
MANAGERS_PATH = HERE / "managers.json"
FAMOUS_PATH = HERE / "famous_investors.json"
DIVIDEND_CORRECTIONS_PATH = HERE / "dividend_corrections.json"


class CuratedError(Exception):
    pass


@dataclass(frozen=True)
class ManagerFund:
    person: str
    ticker: str
    fund_name: str
    since_text: str
    tenure_start: date
    source_url: str
    evidence: str
    verified: date


@dataclass(frozen=True)
class FamousInvestor:
    cik: str
    filer_name: str
    person: str
    role: str
    source_url: str
    verified: date


@dataclass(frozen=True)
class DividendCorrection:
    ticker: str
    ex_date: date
    action: str            # "remove" | "set"
    amount: float | None
    source_url: str
    evidence: str
    verified: date


def tenure_start(since_text: str) -> date:
    """M3: year only -> Dec 31; year-month -> last day of month; full date -> that day."""
    if re.fullmatch(r"\d{4}", since_text):
        return date(int(since_text), 12, 31)
    if m := re.fullmatch(r"(\d{4})-(\d{2})", since_text):
        y, mo = int(m[1]), int(m[2])
        return date(y, mo, calendar.monthrange(y, mo)[1])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since_text):
        return date.fromisoformat(since_text)
    raise CuratedError(f"since must be YYYY, YYYY-MM or YYYY-MM-DD, got {since_text!r}")


def _common(row: dict, today: date, where: str) -> date:
    if not str(row.get("source_url", "")).startswith("https://"):
        raise CuratedError(f"{where}: source_url must be an https link")
    try:
        verified = date.fromisoformat(row["verified"])
    except (KeyError, ValueError):
        raise CuratedError(f"{where}: verified must be YYYY-MM-DD") from None
    if verified > today:
        raise CuratedError(f"{where}: verified date is in the future")
    for k in ("person",):
        if not str(row.get(k, "")).strip():
            raise CuratedError(f"{where}: {k} is required")
    return verified


def load_managers(path: Path, today: date) -> list[ManagerFund]:
    out, seen = [], set()
    for i, r in enumerate(json.loads(Path(path).read_text())):
        where = f"managers.json entry {i}"
        verified = _common(r, today, where)
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", r.get("ticker", "")):
            raise CuratedError(f"{where}: ticker must be upper-case")
        for k in ("fund_name", "evidence", "since"):
            if not str(r.get(k, "")).strip():
                raise CuratedError(f"{where}: {k} is required")
        key = (r["person"], r["ticker"])
        if key in seen:
            raise CuratedError(f"{where}: duplicate {key}")
        seen.add(key)
        out.append(ManagerFund(r["person"].strip(), r["ticker"], r["fund_name"].strip(), r["since"],
                               tenure_start(r["since"]), r["source_url"], r["evidence"].strip(), verified))
    return out


def load_famous(path: Path, today: date) -> list[FamousInvestor]:
    out, seen = [], set()
    for i, r in enumerate(json.loads(Path(path).read_text())):
        where = f"famous_investors.json entry {i}"
        verified = _common(r, today, where)
        if not re.fullmatch(r"[1-9]\d{0,9}", str(r.get("cik", ""))):
            raise CuratedError(f"{where}: cik must be digits without leading zeros")
        for k in ("filer_name", "role"):
            if not str(r.get(k, "")).strip():
                raise CuratedError(f"{where}: {k} is required")
        if r["cik"] in seen:
            raise CuratedError(f"{where}: duplicate cik {r['cik']}")
        seen.add(r["cik"])
        out.append(FamousInvestor(r["cik"], r["filer_name"].strip(), r["person"].strip(), r["role"].strip(),
                                  r["source_url"], verified))
    return out


def load_dividend_corrections(path: Path, today: date) -> list[DividendCorrection]:
    out, seen = [], set()
    for i, r in enumerate(json.loads(Path(path).read_text())):
        where = f"dividend_corrections.json entry {i}"
        if not str(r.get("source_url", "")).startswith("https://"):
            raise CuratedError(f"{where}: source_url must be an https link")
        try:
            verified = date.fromisoformat(r["verified"])
        except (KeyError, ValueError, TypeError):
            raise CuratedError(f"{where}: verified must be YYYY-MM-DD") from None
        if verified > today:
            raise CuratedError(f"{where}: verified date is in the future")
        try:
            ex = date.fromisoformat(r["ex_date"])
        except (KeyError, ValueError, TypeError):
            raise CuratedError(f"{where}: ex_date must be YYYY-MM-DD") from None
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", str(r.get("ticker", ""))):
            raise CuratedError(f"{where}: ticker must be upper-case")
        if not str(r.get("evidence", "")).strip():
            raise CuratedError(f"{where}: evidence is required")
        action, amount = r.get("action"), None
        if action == "set":
            amount = r.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0:
                raise CuratedError(f"{where}: action 'set' needs a non-negative numeric amount")
            amount = float(amount)
        elif action == "remove":
            if "amount" in r:
                raise CuratedError(f"{where}: action 'remove' takes no amount")
        else:
            raise CuratedError(f"{where}: action must be 'remove' or 'set', got {action!r}")
        if (r["ticker"], ex) in seen:
            raise CuratedError(f"{where}: duplicate {(r['ticker'], r['ex_date'])}")
        seen.add((r["ticker"], ex))
        out.append(DividendCorrection(r["ticker"], ex, action, amount, r["source_url"], r["evidence"].strip(), verified))
    return out


def is_stale(verified: date, today: date) -> bool:
    return (today - verified).days > 365
