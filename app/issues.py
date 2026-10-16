"""Data issues (spec §11): deduplicated by (source, kind, ref); resolved when the ref is re-processed cleanly."""
from __future__ import annotations

from app.db import now_iso


def clear_ref(conn, source: str, ref: str) -> None:
    """Call before re-processing `ref`; any issue not recorded again stays resolved."""
    conn.execute("UPDATE issues SET resolved=1 WHERE source=? AND ref=?", (source, ref))


def record(conn, source: str, kind: str, ref: str, message: str, url: str = "") -> None:
    now = now_iso()
    conn.execute(
        """INSERT INTO issues(source, kind, ref, message, url, first_seen, last_seen, resolved)
           VALUES(?,?,?,?,?,?,?,0)
           ON CONFLICT(source, kind, ref) DO UPDATE SET message=excluded.message, url=excluded.url,
             last_seen=excluded.last_seen, resolved=0""",
        (source, kind, ref, message, url, now, now))


def open_issues(conn, source: str | None = None, kind: str | None = None, offset: int = 0,
                limit: int | None = None) -> list[dict]:
    """Open issues, optionally of one source and kind, ordered by source, kind and ref; `limit` rows from `offset`."""
    where, args = ["resolved=0"], []
    for col, v in (("source", source), ("kind", kind)):
        if v is not None:
            where.append(f"{col}=?")
            args.append(v)
    rows = conn.execute("SELECT source, kind, ref, message, url, first_seen, last_seen FROM issues "
                        f"WHERE {' AND '.join(where)} ORDER BY source, kind, ref LIMIT ? OFFSET ?",
                        (*args, -1 if limit is None else limit, offset))
    return [dict(r) for r in rows]


def issue_counts(conn) -> list[dict]:
    rows = conn.execute("SELECT source, kind, COUNT(*) AS count FROM issues WHERE resolved=0 "
                        "GROUP BY source, kind ORDER BY source, kind")
    return [dict(r) for r in rows]
