"""Local HTTP server: the page and a small JSON API (spec §14). Binds to 127.0.0.1 only."""
from __future__ import annotations

import json
import logging
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from app import db
from app.config import PROJECT_ROOT
from app.issues import issue_counts, open_issues
from app.leaderboards import WINDOWS, strip_detail
from app.pipeline import read_detail
from app.prices import NY
from app.scheduler import STALE_AFTER

log = logging.getLogger("etf.server")
GROUPS = {"managers", "investors", "congress"}
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8")}
SECURITY = {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
MARKET_FRESH = timedelta(minutes=15)     # live prices come every 5 minutes; older than this means they stopped
ISSUES_PAGE, ISSUES_PAGE_MAX = 200, 1000     # issues per request; the page reads one kind at a time


def make_server(connect: Callable, port: int, progress: dict | None = None,
                web_dir: Path = PROJECT_ROOT / "web", now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                senate_enabled: bool = False) -> ThreadingHTTPServer:
    cache: dict[tuple[str, str], tuple[int, dict]] = {}
    progress = progress if progress is not None else {}

    def board(c, group: str, win: str) -> dict | None:
        row = c.execute("SELECT data_version FROM leaderboards WHERE grp=? AND win=?", (group, win)).fetchone()
        if row is None:
            return None
        hit = cache.get((group, win))
        if hit and hit[0] == row["data_version"]:
            return hit[1]
        row = c.execute("SELECT json, data_version FROM leaderboards WHERE grp=? AND win=?", (group, win)).fetchone()
        b = json.loads(row["json"])
        cache[(group, win)] = (row["data_version"], b)
        return b

    def in_session(at: datetime) -> bool:
        et = at.astimezone(NY)
        return et.weekday() < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)

    def person(c, group: str, win: str, row: dict) -> dict:
        """The board row with its detail. build_boards stores each whole row (detail included, rank not yet set
        for 13F investors) in board_details; keys missing there (rank) are appended, as rank_rows appended them,
        so the response is the same row, in the same key order, as when details were inside the board."""
        full = read_detail(c, group, win, str(row["id"]))
        if full is None:
            return row                     # a board stored before the detail table (details still inline)
        for k, v in row.items():
            full.setdefault(k, v)
        return full

    def status(c) -> dict:
        p = dict(progress)        # a copy: job threads add and remove keys while this runs
        at = now()
        market = json.loads(db.get_meta(c, "market", '{"open": false, "as_of": null}'))
        # The live job's verdict is shown only while it is fresh and the regular session is on (holidays are
        # already covered by the live job itself, which then stores open = false).
        session = bool(market.get("open")) and in_session(at)
        fresh = bool(market.get("as_of")) and abs(at - datetime.fromisoformat(market["as_of"])) <= MARKET_FRESH
        market["open"] = session and fresh
        sources = {}
        for name, s in db.source_status(c).items():
            age = (at - datetime.fromisoformat(s["last_success"])).total_seconds() if s["last_success"] else None
            limit = STALE_AFTER.get(name)
            stale = age is None or (limit is not None and age > limit.total_seconds()
                                    and (name != "prices" or session))    # stopped live prices show as stale
            sources[name] = {**s, "age_seconds": age, "stale": stale}
        n_issues = c.execute("SELECT COUNT(*) FROM issues WHERE resolved=0").fetchone()[0]
        return {"data_version": int(db.get_meta(c, "data_version", "0")), "sources": sources, "market": market,
                "backfill": p.get("backfill"), "progress": {k: v for k, v in p.items() if k != "backfill"},
                "issues": n_issues, "backfill_done": bool(db.get_meta(c, "backfill_done")),
                "senate_enabled": senate_enabled}

    port_holder: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            host = (self.headers.get("Host") or "").lower()
            if host not in (f"127.0.0.1:{port_holder[0]}", f"localhost:{port_holder[0]}"):
                return self._json({"error": "bad host"}, 400)
            try:
                self._route()
            except (BrokenPipeError, ConnectionResetError):
                pass                     # the browser closed the connection before the answer was sent
            except Exception:
                logging.exception("request failed: %s", self.path)
                try:
                    self._json({"error": "internal error"}, 500)
                except Exception:
                    pass

        def _route(self):
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in STATIC:
                name, ctype = STATIC[u.path]
                return self._send(200, (web_dir / name).read_bytes(), ctype)
            with closing(connect()) as c:       # one per request: each request runs on a new thread
                return self._api(c, u.path, q)

        def _api(self, c, path: str, q: dict):
            if path == "/api/status":
                return self._json(status(c))
            if path == "/api/issues/summary":
                return self._json(issue_counts(c))
            if path == "/api/issues":
                try:
                    offset, limit = int(q.get("offset", "0")), int(q.get("limit", str(ISSUES_PAGE)))
                except ValueError:
                    return self._json({"error": "offset and limit must be whole numbers"}, 400)
                if offset < 0 or not 0 < limit <= ISSUES_PAGE_MAX:
                    return self._json({"error": f"offset must be >= 0 and limit 1-{ISSUES_PAGE_MAX}"}, 400)
                return self._json(open_issues(c, q.get("source"), q.get("kind"), offset, limit))
            if path in ("/api/leaderboard", "/api/person"):
                group, win = q.get("group"), q.get("window")
                if group not in GROUPS or win not in WINDOWS:
                    return self._json({"error": "group must be managers|investors|congress, window 1y|3y|5y"}, 400)
                b = board(c, group, win)
                if b is None:
                    return self._json({"error": "not computed yet"}, 404)
                if path == "/api/leaderboard":
                    return self._json(strip_detail(b))
                row = next((r for r in b["ranked"] + b["unranked"] if str(r["id"]) == q.get("id")), None)
                return self._json(person(c, group, win, row)) if row else self._json({"error": "no such person"}, 404)
            self._json({"error": "not found"}, 404)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port_holder.append(srv.server_address[1])
    return srv
