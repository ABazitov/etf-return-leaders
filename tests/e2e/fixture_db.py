"""Builds a small database with boards for every tab/window, including hostile text, for browser tests."""
import json
from datetime import datetime, timezone
from pathlib import Path

from app import db, issues
from app.leaderboards import strip_detail

EVIL = '<img src=x onerror="document.title=\'pwned\'">'
PERIOD = {"period": "2026-06-30", "etf_value_usd": 2.5e8, "etf_share": 0.042, "url": "https://example.com/13f",
          "positions": [{"ticker": "IAU", "cusip": "464285204", "name": "ISHARES GOLD", "shares": 1.0,
                         "reported_usd": 2.5e8, "value_usd": 2.5e8, "priced": True}]}
PERIOD_HASH = "e2e-period-1"


def row(i, name, total, **extra):
    base = {"id": f"p{i}", "name": name, "rank": i, "total": total, "annualized": total / 3, "vs_spx": (total - 0.1) * 100,
            "badge": extra.pop("badge", "EXACT"),
            "detail": {"series": [["2025-10-08", 1.0], ["2026-04-08", 1 + total / 2], ["2026-10-08", 1 + total]],
                       "funds": [{"ticker": "AAA", "fund_name": "A Fund", "since": "2014", "total": total, "counts": True,
                                  "reason": "", "source_url": "https://example.com/p.pdf", "evidence": "since 2014",
                                  "verified": "2026-10-08"}]}}
    base.update(extra)
    return base


def build(path: Path) -> None:
    conn = db.connect(path)
    with db.transaction(conn):
        for group in ("managers", "investors", "congress"):
            for win, years in (("1y", 1), ("3y", 3), ("5y", 5)):
                extra = {"investors": {"badge": "APPROX", "etf_value_usd": 2.5e8, "etf_share": 0.042, "etf_share_avg": 0.05,
                                       "n_etfs": 3, "top_etf": "IAU", "person": ""},
                         "congress": {"badge": "EST", "party_state": "D-CA", "chamber": "House", "n_trades": 4,
                                      "top_etf": "RSP", "low": 0.1, "high": 0.3}}.get(group, {"funds": ["AAA"]})
                ranked = [row(1, f"Alpha {group} {win}", 0.42 * years, **extra), row(2, EVIL, -0.05, **extra),
                          row(3, "Gamma Person", 0.01, **extra)]
                if group == "managers":
                    ranked[2]["detail"]["funds"][0]["source_url"] = "javascript:alert(1)"
                if win == "1y":
                    for r in ranked:
                        r["annualized"] = None
                b = {"group": group, "window": win, "years": years, "start": "2025-10-08", "end": "2026-10-08",
                     "benchmark": {"ticker": "SPY", "total": 0.1,
                                   "series": [["2025-10-08", 1.0], ["2026-10-08", 1.1]]},
                     "as_of": "2026-10-08T17:00:00-04:00", "ranked": ranked,
                     "unranked": [{"id": "u1", "name": "Delta Person", "reason": "invested on 15.6% of trading days",
                                   "badge": extra.get("badge", "EXACT"), "detail": {}}]}
                if group == "investors":                   # periods stored once, listed by hash (see _PeriodStore)
                    for r in ranked:
                        r["detail"] = {"periods": [PERIOD_HASH], "series": r["detail"]["series"]}
                    conn.execute("INSERT OR REPLACE INTO board_periods VALUES(?,?)", (PERIOD_HASH, json.dumps(PERIOD)))
                for r in b["ranked"] + b["unranked"]:      # as build_boards stores them: whole rows, in their own table
                    conn.execute("INSERT OR REPLACE INTO board_details VALUES(?,?,?,?,1)",
                                 (group, win, str(r["id"]), json.dumps(r)))
                conn.execute("INSERT OR REPLACE INTO leaderboards VALUES(?,?,?,?,1)",
                             (group, win, json.dumps(strip_detail(b)), "x"))
        db.set_meta(conn, "data_version", "1")
        db.set_meta(conn, "market", json.dumps({"open": True, "as_of": datetime.now(timezone.utc).isoformat()}))
        db.set_meta(conn, "backfill_done", "x")
        for s in ("prices", "house", "sec13f", "senate"):
            db.set_source_status(conn, s, True)
        issues.record(conn, "house", "scanned", "H9116249", "Hon. Example (OK04): scanned", "https://example.com/9116249.pdf")
