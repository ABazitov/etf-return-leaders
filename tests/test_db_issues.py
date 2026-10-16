import sqlite3

import pytest

from app import db, issues


def test_schema_has_all_tables(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "prices_daily", "prices_live", "securities", "cusip_map", "f13_filings", "f13_holdings",
            "f13_filing_totals", "congress_reports", "congress_trades", "members", "issues",
            "source_status", "leaderboards", "managers", "famous_investors"} <= names


def test_transaction_rolls_back_on_error(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    try:
        with db.transaction(conn):
            db.set_meta(conn, "x", "1")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert db.get_meta(conn, "x") is None


def test_version_bumps(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.bump_version(conn) == 1
    assert db.bump_version(conn) == 2


def test_source_status_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.set_source_status(conn, "house", True, counters={"reports": 3})
    db.set_source_status(conn, "senate", False, error="403")
    st = db.source_status(conn)
    assert st["house"]["last_success"] and st["house"]["counters"] == {"reports": 3}
    assert st["senate"]["last_success"] is None and st["senate"]["last_error"] == "403"


def test_connection_waits_out_a_board_rebuild_and_caps_the_wal(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # a full board rebuild holds the write lock for about two minutes; other jobs must wait, not fail
    assert db.BUSY_TIMEOUT_S >= 600
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_S * 1000
    assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == db.WAL_SIZE_LIMIT


def test_issue_dedupe_and_resolve_per_ref(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    issues.record(conn, "house", "scanned", "9116249", "scanned report", "https://x/9116249.pdf")
    issues.record(conn, "house", "scanned", "9116249", "scanned report", "https://x/9116249.pdf")
    issues.record(conn, "house", "rejected", "20034106", "format")
    assert len(issues.open_issues(conn)) == 2
    # re-processing 20034106 without an issue resolves it; 9116249 untouched
    issues.clear_ref(conn, "house", "20034106")
    open_ = issues.open_issues(conn)
    assert [i["ref"] for i in open_] == ["9116249"]
    # recording again re-opens
    issues.record(conn, "house", "rejected", "20034106", "format")
    assert len(issues.open_issues(conn)) == 2


def test_open_issues_counted_by_kind_and_read_in_pages(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    for i in range(5):
        issues.record(conn, "leaderboard", "x2-mismatch", f"r{i}", f"m{i}")
    issues.record(conn, "house", "scanned", "H1", "scanned report", "https://x")
    issues.record(conn, "house", "rejected", "H2", "format")
    issues.clear_ref(conn, "house", "H2")
    assert issues.issue_counts(conn) == [{"source": "house", "kind": "scanned", "count": 1},
                                         {"source": "leaderboard", "kind": "x2-mismatch", "count": 5}]
    page = issues.open_issues(conn, source="leaderboard", kind="x2-mismatch", offset=1, limit=2)
    assert [i["ref"] for i in page] == ["r1", "r2"]
    assert [i["ref"] for i in issues.open_issues(conn, limit=1)] == ["H1"]
    assert len(issues.open_issues(conn)) == 6


def test_congress_trades_filing_status_column_and_migration(tmp_path):
    import sqlite3
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO congress_trades(report_id, row) VALUES('H1', 1)")
    assert conn.execute("SELECT filing_status FROM congress_trades").fetchone()[0] == "New"
    old = sqlite3.connect(str(tmp_path / "old.db"))          # a database made before the column existed
    old.execute("""CREATE TABLE congress_trades (report_id TEXT, row INTEGER, owner TEXT, asset_text TEXT,
                   asset_type TEXT, ticker TEXT, tx_type TEXT, tx_date TEXT, amount_text TEXT, is_etf INTEGER,
                   is_option INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (report_id, row))""")
    old.execute("INSERT INTO congress_trades(report_id, row) VALUES('H1', 1)")
    old.commit()
    old.close()
    conn = db.connect(tmp_path / "old.db")
    assert conn.execute("SELECT filing_status FROM congress_trades").fetchone()[0] == "New"
    db.connect(tmp_path / "old.db")                           # a second open does not add it again


def test_read_only_connection_reads_rows_by_name_and_cannot_write(tmp_path):
    path = tmp_path / "STOCKS TEST" / "t.db"                     # a space, as in the real project path
    db.connect(path).close()
    ro = db.connect_read_only(path)
    assert ro.execute("SELECT COUNT(*) AS n FROM meta").fetchone()["n"] == 0
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("INSERT INTO meta(key, value) VALUES('k', 'v')")
