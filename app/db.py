"""SQLite storage (spec §6)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS prices_daily (
  ticker TEXT, day TEXT, close REAL NOT NULL, dividend REAL NOT NULL DEFAULT 0, split REAL NOT NULL DEFAULT 1,
  adjclose REAL, PRIMARY KEY (ticker, day));
CREATE TABLE IF NOT EXISTS prices_live (ticker TEXT PRIMARY KEY, price REAL NOT NULL, as_of TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS securities (
  ticker TEXT PRIMARY KEY, name TEXT, yahoo_type TEXT, figi_type TEXT, is_etf INTEGER NOT NULL,
  price_ok INTEGER NOT NULL DEFAULT 1, evidence TEXT, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cusip_map (
  cusip TEXT PRIMARY KEY, ticker TEXT, figi_type TEXT, exch TEXT, raw TEXT, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS f13_filings (
  accession TEXT PRIMARY KEY, cik TEXT NOT NULL, filer_name TEXT, period TEXT NOT NULL, filed TEXT NOT NULL,
  form TEXT NOT NULL, amendment_type TEXT);
CREATE INDEX IF NOT EXISTS f13_filings_cik ON f13_filings (cik, period);
CREATE TABLE IF NOT EXISTS f13_holdings (
  accession TEXT, cusip TEXT, shares REAL NOT NULL, value_usd REAL NOT NULL, issuer TEXT, title TEXT,
  PRIMARY KEY (accession, cusip));
CREATE INDEX IF NOT EXISTS f13_holdings_cusip ON f13_holdings (cusip);
CREATE TABLE IF NOT EXISTS f13_filing_totals (accession TEXT PRIMARY KEY, n_rows INTEGER, total_value_usd REAL);
CREATE TABLE IF NOT EXISTS f13_books (accession TEXT PRIMARY KEY, book_usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS f13_period_stats (
  cik TEXT, period TEXT, etf_usd REAL NOT NULL, book_usd REAL, PRIMARY KEY (cik, period));
CREATE TABLE IF NOT EXISTS congress_reports (
  report_id TEXT PRIMARY KEY, chamber TEXT NOT NULL, filer_name TEXT, member_key TEXT, filed TEXT,
  url TEXT, status TEXT NOT NULL, reason TEXT, amends TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS congress_trades (
  report_id TEXT, row INTEGER, owner TEXT, asset_text TEXT, asset_type TEXT, ticker TEXT,
  tx_type TEXT, tx_date TEXT, amount_text TEXT, is_etf INTEGER, is_option INTEGER NOT NULL DEFAULT 0,
  filing_status TEXT NOT NULL DEFAULT 'New',
  PRIMARY KEY (report_id, row));
CREATE TABLE IF NOT EXISTS members (
  member_key TEXT PRIMARY KEY, name TEXT, chamber TEXT, party TEXT, state TEXT, district INTEGER, bioguide TEXT);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, kind TEXT, ref TEXT, message TEXT, url TEXT,
  first_seen TEXT, last_seen TEXT, resolved INTEGER NOT NULL DEFAULT 0, UNIQUE (source, kind, ref));
CREATE TABLE IF NOT EXISTS source_status (
  source TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, last_error_at TEXT, counters TEXT);
CREATE TABLE IF NOT EXISTS leaderboards (
  grp TEXT, win TEXT, json TEXT NOT NULL, computed_at TEXT NOT NULL, data_version INTEGER,
  PRIMARY KEY (grp, win));
CREATE TABLE IF NOT EXISTS board_periods (h TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS board_details (
  grp TEXT, win TEXT, id TEXT, json TEXT NOT NULL, data_version INTEGER, PRIMARY KEY (grp, win, id));
CREATE TABLE IF NOT EXISTS managers (person TEXT, ticker TEXT, fund_name TEXT, since TEXT, source_url TEXT,
  evidence TEXT, verified TEXT, PRIMARY KEY (person, ticker));
CREATE TABLE IF NOT EXISTS famous_investors (cik TEXT PRIMARY KEY, filer_name TEXT, person TEXT, role TEXT,
  source_url TEXT, verified TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A board rebuild over the full data set holds the write lock for about two minutes (it rewrites ~2 GB of details);
# a job that needs to write meanwhile waits for it instead of failing. Readers never wait (WAL).
BUSY_TIMEOUT_S = 600
WAL_SIZE_LIMIT = 64 * 1024 * 1024    # bytes the WAL file is cut back to after a checkpoint; unset, it keeps its peak size


def connect(path: Path | str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def connect_read_only(path: Path | str) -> sqlite3.Connection:
    """For the web server, which only reads: no schema script on each request, and no write can slip through."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, isolation_level=None,
                           check_same_thread=False, timeout=BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Columns added after a table was first created; CREATE TABLE IF NOT EXISTS leaves older tables as they were."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(congress_trades)")}
    if "filing_status" not in cols:
        conn.execute("ALTER TABLE congress_trades ADD COLUMN filing_status TEXT NOT NULL DEFAULT 'New'")


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def bump_version(conn) -> int:
    v = int(get_meta(conn, "data_version", "0")) + 1
    set_meta(conn, "data_version", str(v))
    return v


def set_source_status(conn, source: str, ok: bool, error: str = "", counters: dict | None = None) -> None:
    now = now_iso()
    conn.execute("INSERT INTO source_status(source) VALUES(?) ON CONFLICT(source) DO NOTHING", (source,))
    if ok:
        conn.execute("UPDATE source_status SET last_success=?, counters=? WHERE source=?",
                     (now, json.dumps(counters or {}), source))
    else:
        conn.execute("UPDATE source_status SET last_error=?, last_error_at=? WHERE source=?", (error, now, source))


def source_status(conn) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM source_status"):
        out[r["source"]] = {
            "last_success": r["last_success"], "last_error": r["last_error"],
            "last_error_at": r["last_error_at"], "counters": json.loads(r["counters"] or "{}"),
        }
    return out
