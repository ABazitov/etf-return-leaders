"""Which securities count as ETFs (E1) and how report text becomes a ticker (E2, E3)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.http import fetch

_OPENFIGI = "https://api.openfigi.com/v3/mapping"
_PAREN_TICKER = re.compile(r"\(([A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z]{1,2})?)\)")
_TOKEN_ETF = re.compile(r"(?:^|\s)([A-Z]{1,5})\s+ETF\b")
_ETF_WORDS = re.compile(
    r"\b(?:etf|ishares|spdr|vanguard|invesco qqq|schwab us|schwab etf|proshares|direxion|ark|global x|wisdomtree|vaneck|"
    r"first trust|index fund|exchange traded)\b", re.I)


@dataclass(frozen=True)
class FigiHit:
    ticker: str
    security_type: str
    name: str
    exch: str


def parse_openfigi(response: list) -> list[FigiHit | None]:
    out: list[FigiHit | None] = []
    for job in response:
        data = job.get("data") or []
        us = [d for d in data if d.get("exchCode") == "US"] or data
        out.append(FigiHit(us[0]["ticker"], us[0].get("securityType") or "", us[0].get("name") or "",
                           us[0].get("exchCode") or "") if us else None)
    return out


class OpenFigiClient:
    """Keyless: 10 jobs per request, ~25 requests/minute. With a key: 100 jobs, ~25 requests per 6 seconds."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.batch = 100 if api_key else 10
        self.interval = 0.25 if api_key else 2.5

    def _map(self, jobs: list[dict]) -> list[FigiHit | None]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key
        out: list[FigiHit | None] = []
        for i in range(0, len(jobs), self.batch):
            body = json.dumps(jobs[i:i + self.batch]).encode()
            resp = fetch(_OPENFIGI, ua="ETF-Return-Leaders/1.0", data=body, headers=headers,
                         min_interval=self.interval)
            out.extend(parse_openfigi(json.loads(resp)))
        return out

    def map_cusips(self, cusips: list[str]) -> list[FigiHit | None]:
        return self._map([{"idType": "ID_CUSIP", "idValue": c} for c in cusips])

    def map_tickers(self, tickers: list[str]) -> list[FigiHit | None]:
        return self._map([{"idType": "TICKER", "idValue": t, "exchCode": "US"} for t in tickers])


def parse_nasdaq_directory(text: str) -> dict[str, tuple[str, bool]]:
    lines = text.splitlines()
    header = lines[0].split("|")
    sym, name, etf, test = (header.index(k) for k in ("Symbol", "Security Name", "ETF", "Test Issue"))
    out = {}
    for line in lines[1:]:
        f = line.split("|")
        if len(f) != len(header) or f[test] == "Y":
            continue
        out[f[sym]] = (f[name], f[etf] == "Y")
    return out


def is_etf(figi_type: str | None, yahoo_type: str | None, name: str) -> bool:
    """E1: OpenFIGI says ETP and Yahoo says ETF, and it is not an exchange-traded note."""
    if figi_type != "ETP" or yahoo_type != "ETF":
        return False
    return not re.search(r"\bETN\b|exchange traded notes?", name, re.I)


def ticker_candidates(asset_text: str, given_ticker: str | None = None) -> list[str]:
    """E3 steps 1-2. A given ticker (Senate column) wins unless it is blank or '--'."""
    if given_ticker and given_ticker.strip() not in ("", "--"):
        return [given_ticker.strip().upper()]
    paren = _PAREN_TICKER.findall(asset_text)
    if paren:
        return [paren[-1]]
    m = _TOKEN_ETF.search(asset_text)
    return [m.group(1)] if m else []


def normalize_name(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.casefold())
    return re.sub(r"\s+", " ", s).strip()


def etf_name_index(directory: dict[str, tuple[str, bool]]) -> dict[str, list[str]]:
    """Normalized ETF name -> its symbols, built once so match_by_name does not rescan the directory."""
    out: dict[str, list[str]] = {}
    for sym, (name, etf) in directory.items():
        if etf:
            out.setdefault(normalize_name(name), []).append(sym)
    return out


def match_by_name(asset_text: str, names: dict[str, list[str]]) -> str | None:
    """E3 step 3: exact normalized match to exactly one ETF name (`names` from etf_name_index)."""
    hits = names.get(normalize_name(asset_text), [])
    return hits[0] if len(hits) == 1 else None


def looks_like_etf(text: str) -> bool:
    return bool(_ETF_WORDS.search(text))
