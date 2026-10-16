import json
import threading
import urllib.error
import urllib.request

import pytest

from app import db, issues
from app.leaderboards import strip_detail
from app.server import make_server

BOARD = {"group": "managers", "window": "1y", "years": 1, "start": "2025-10-08", "end": "2026-10-08",
         "benchmark": {"ticker": "SPY", "total": 0.1}, "as_of": "x",
         "ranked": [{"id": "ann", "name": "Ann", "total": 0.2, "badge": "EXACT", "detail": {"funds": []}, "rank": 1}],
         "unranked": [{"id": "bob", "name": "Bob", "reason": "no fund managed for the whole window", "detail": {}}]}


@pytest.fixture
def base(tmp_path):
    path = tmp_path / "t.db"
    conn = db.connect(path)
    with db.transaction(conn):
        conn.execute("INSERT INTO leaderboards VALUES('managers','1y',?, 'x', 1)", (json.dumps(strip_detail(BOARD)),))
        for r in BOARD["ranked"] + BOARD["unranked"]:      # stored as build_boards does: rows with detail, no rank
            conn.execute("INSERT INTO board_details VALUES('managers','1y',?,?,1)",
                         (r["id"], json.dumps({k: v for k, v in r.items() if k != "rank"})))
        db.set_meta(conn, "data_version", "1")
        db.set_source_status(conn, "house", True)
        issues.record(conn, "house", "scanned", "H1", "scanned report", "https://x")
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<title>ETF Return Leaders</title>")
    (web / "app.js").write_text("1")
    (web / "styles.css").write_text("a{}")
    srv = make_server(lambda: db.connect_read_only(path), 0, progress={}, web_dir=web)   # as main.py does
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers, r.read()


def test_binds_localhost_only(base):
    assert base.startswith("http://127.0.0.1:")


def test_page_and_headers(base):
    status, headers, body = get(base + "/")
    assert status == 200 and b"ETF Return Leaders" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert get(base + "/app.js")[1]["Content-Type"].startswith("text/javascript")


def test_leaderboard_strips_detail(base):
    _, _, body = get(base + "/api/leaderboard?group=managers&window=1y")
    b = json.loads(body)
    assert b["ranked"][0]["name"] == "Ann" and "detail" not in b["ranked"][0]


def test_person_has_detail(base):
    _, _, body = get(base + "/api/person?group=managers&window=1y&id=bob")
    assert json.loads(body)["reason"].startswith("no fund")


def test_status_and_issues(base):
    st = json.loads(get(base + "/api/status")[2])
    assert st["data_version"] == 1 and st["issues"] == 1 and st["sources"]["house"]["stale"] is False
    assert json.loads(get(base + "/api/issues")[2])[0]["kind"] == "scanned"


def test_issues_summary_and_pages(base):
    """Hundreds of thousands of issues can be open: the page gets counts per kind and reads each kind in pages."""
    assert json.loads(get(base + "/api/issues/summary")[2]) == [{"source": "house", "kind": "scanned", "count": 1}]
    page = json.loads(get(base + "/api/issues?source=house&kind=scanned&offset=0&limit=10")[2])
    assert [(i["ref"], i["url"]) for i in page] == [("H1", "https://x")]
    assert json.loads(get(base + "/api/issues?source=house&kind=scanned&offset=1")[2]) == []


@pytest.mark.parametrize("path,code", [("/api/leaderboard?group=x&window=1y", 400), ("/nope", 404),
                                       ("/api/person?group=managers&window=1y&id=zzz", 404),
                                       ("/../config.toml", 404), ("/api/issues?limit=x", 400),
                                       ("/api/issues?offset=-1", 400), ("/api/issues?limit=0", 400)])
def test_errors(base, path, code):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + path)
    assert e.value.code == code


def test_bad_host_rejected_and_good_host_ok(base):
    import http.client
    port = int(base.rsplit(":", 1)[1])
    for host, code in [("evil.example", 400), (f"localhost:{port}", 200), (f"127.0.0.1:{port}", 200)]:
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("GET", "/api/issues", headers={"Host": host})
        assert c.getresponse().status == code


def test_internal_error_is_json_500(tmp_path):
    web = tmp_path / "w"
    web.mkdir()

    def boom():
        raise RuntimeError("x")
    srv = make_server(boom, 0, web_dir=web)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(f"http://127.0.0.1:{srv.server_address[1]}/api/status")
        assert e.value.code == 500 and json.loads(e.value.read()) == {"error": "internal error"}
    finally:
        srv.shutdown()


# ---------- final review: market status (item 6) and the Senate flag (item 14) ----------

from datetime import datetime, timedelta, timezone

from app.prices import NY


def _status_server(tmp_path, now, market, senate_enabled=False):
    path = tmp_path / "s.db"
    conn = db.connect(path)
    with db.transaction(conn):
        db.set_meta(conn, "market", json.dumps(market))
    srv = make_server(lambda: db.connect(path), 0, progress={}, web_dir=tmp_path, now=lambda: now,
                      senate_enabled=senate_enabled)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return json.loads(get(f"http://127.0.0.1:{srv.server_address[1]}/api/status")[2])
    finally:
        srv.shutdown()


WED_1432 = datetime(2026, 10, 7, 14, 32, tzinfo=NY)


@pytest.mark.parametrize("now,stored_open,as_of,expect", [
    (WED_1432, True, WED_1432 - timedelta(minutes=3), True),
    (WED_1432, True, WED_1432 - timedelta(minutes=16), False),           # the live job stopped updating
    (WED_1432, False, WED_1432 - timedelta(minutes=3), False),
    (WED_1432.replace(hour=16, minute=1), True, WED_1432.replace(hour=15, minute=55), False),   # after the close
    (WED_1432.replace(hour=9, minute=29), True, WED_1432.replace(hour=9, minute=25), False),    # before the open
    (WED_1432 + timedelta(days=3), True, WED_1432 + timedelta(days=3, minutes=-3), False),      # Saturday
    (WED_1432, True, None, False),
])
def test_market_open_only_when_fresh_and_in_session(tmp_path, now, stored_open, as_of, expect):
    st = _status_server(tmp_path, now.astimezone(timezone.utc), {"open": stored_open,
                                                                "as_of": as_of.isoformat() if as_of else None})
    assert st["market"]["open"] is expect


def test_status_reports_whether_the_senate_is_enabled(tmp_path):
    assert _status_server(tmp_path, WED_1432, {"open": False, "as_of": None})["senate_enabled"] is False
    assert _status_server(tmp_path, WED_1432, {"open": False, "as_of": None}, senate_enabled=True)["senate_enabled"] is True


def test_stopped_live_prices_show_closed_but_stale(tmp_path):
    path = tmp_path / "s.db"
    conn = db.connect(path)
    with db.transaction(conn):
        db.set_meta(conn, "market", json.dumps({"open": True, "as_of": (WED_1432 - timedelta(minutes=30)).isoformat()}))
        db.set_source_status(conn, "prices", True)
        conn.execute("UPDATE source_status SET last_success=? WHERE source='prices'",
                     ((WED_1432 - timedelta(minutes=30)).isoformat(),))
    srv = make_server(lambda: db.connect(path), 0, progress={}, web_dir=tmp_path, now=lambda: WED_1432)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        st = json.loads(get(f"http://127.0.0.1:{srv.server_address[1]}/api/status")[2])
    finally:
        srv.shutdown()
    assert st["market"]["open"] is False and st["sources"]["prices"]["stale"] is True


# ---------- detail table ----------

def test_person_response_is_the_full_row_with_detail(base):
    _, _, body = get(base + "/api/person?group=managers&window=1y&id=ann")
    assert body == json.dumps(BOARD["ranked"][0]).encode()   # same keys and order: rank_rows appends rank last
    _, _, body = get(base + "/api/person?group=managers&window=1y&id=bob")
    assert body == json.dumps(BOARD["unranked"][0]).encode()


def test_stored_board_has_no_detail_and_leaderboard_is_unchanged(base, tmp_path):
    conn = db.connect(tmp_path / "t.db")
    stored = json.loads(conn.execute("SELECT json FROM leaderboards").fetchone()[0])
    assert all("detail" not in r for r in stored["ranked"] + stored["unranked"])
    assert json.loads(get(base + "/api/leaderboard?group=managers&window=1y")[2]) == strip_detail(BOARD)


def test_board_stored_in_the_old_format_still_serves_detail(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    with db.transaction(conn):
        conn.execute("INSERT INTO leaderboards VALUES('managers','1y',?, 'x', 1)", (json.dumps(BOARD),))
    srv = make_server(lambda: db.connect(path), 0, progress={}, web_dir=tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        u = f"http://127.0.0.1:{srv.server_address[1]}"
        assert json.loads(get(u + "/api/person?group=managers&window=1y&id=ann")[2]) == BOARD["ranked"][0]
        assert "detail" not in json.loads(get(u + "/api/leaderboard?group=managers&window=1y")[2])["ranked"][0]
    finally:
        srv.shutdown()


def test_investor_detail_periods_are_put_back_from_their_store(tmp_path):
    """build_boards stores each 13F period once (board_periods) and the detail row lists them by hash."""
    path = tmp_path / "p.db"
    conn = db.connect(path)
    q2 = {"period": "2026-06-30", "etf_value_usd": 2.0, "url": "u2", "positions": [{"ticker": "B", "shares": 2.0}]}
    q1 = {"period": "2026-03-31", "etf_value_usd": 1.0, "url": "u1", "positions": [{"ticker": "A", "shares": 1.0}]}
    row = {"id": "77", "name": "Filer", "total": 0.1, "badge": "APPROX", "rank": 1}
    board = {**BOARD, "group": "investors", "ranked": [row], "unranked": []}
    with db.transaction(conn):
        conn.execute("INSERT INTO leaderboards VALUES('investors','1y',?, 'x', 1)", (json.dumps(board),))
        conn.executemany("INSERT INTO board_periods VALUES(?,?)", [("h1", json.dumps(q1)), ("h2", json.dumps(q2))])
        stored = {k: v for k, v in row.items() if k != "rank"} | {"detail": {"periods": ["h1", "h2"], "series": []}}
        conn.execute("INSERT INTO board_details VALUES('investors','1y','77',?,1)", (json.dumps(stored),))
    srv = make_server(lambda: db.connect(path), 0, progress={}, web_dir=tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        u = f"http://127.0.0.1:{srv.server_address[1]}"
        got = json.loads(get(u + "/api/person?group=investors&window=1y&id=77")[2])
        assert got == {**stored, "detail": {"periods": [q1, q2], "series": []}, "rank": 1}
        assert list(got) == ["id", "name", "total", "badge", "detail", "rank"]
    finally:
        srv.shutdown()


# ---------- 2026-10-15 audit ----------

def _serve(tmp_path, connect, progress=None):
    srv = make_server(connect, 0, progress={} if progress is None else progress, web_dir=tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_status_does_not_walk_the_progress_dict_a_job_is_changing(tmp_path):
    """The job threads add and remove progress keys while the server builds the status. Simulated by a dict that
    gains a key while it is walked: walking it raises "dictionary changed size during iteration"."""
    class Busy(dict):
        def items(self):
            for kv in super().items():
                self[f"job{len(self)}"] = "running"
                yield kv
    path = tmp_path / "s.db"
    db.connect(path).close()
    srv, u = _serve(tmp_path, lambda: db.connect_read_only(path), Busy(house={"parsed": 1}, backfill={"step": 2}))
    try:
        st = json.loads(get(u + "/api/status")[2])
    finally:
        srv.shutdown()
    assert st["progress"] == {"house": {"parsed": 1}} and st["backfill"] == {"step": 2}


def test_a_closed_connection_is_not_logged_as_a_failed_request(tmp_path, caplog):
    """The browser closing the tab before the answer is sent is not a server error (BrokenPipeError on write)."""
    def gone():
        raise BrokenPipeError(32, "Broken pipe")
    srv, u = _serve(tmp_path, gone)
    try:
        with pytest.raises(Exception):                 # no answer is sent to a client that has gone
            get(u + "/api/status")
    finally:
        srv.shutdown()
    assert "request failed" not in caplog.text


def test_each_api_request_closes_its_connection_and_static_files_open_none(tmp_path):
    path = tmp_path / "s.db"
    db.connect(path).close()
    (tmp_path / "index.html").write_text("<title>x</title>")
    opened = []

    def connect():
        opened.append(db.connect_read_only(path))
        return opened[-1]
    srv, u = _serve(tmp_path, connect)
    try:
        get(u + "/")
        assert opened == []
        get(u + "/api/status")
        get(u + "/api/issues/summary")
    finally:
        srv.shutdown()
    assert len(opened) == 2
    for c in opened:
        with pytest.raises(Exception, match="closed"):
            c.execute("SELECT 1")


def test_a_rebuilt_board_is_served_not_the_cached_one(tmp_path):
    path = tmp_path / "s.db"
    conn = db.connect(path)
    with db.transaction(conn):
        conn.execute("INSERT INTO leaderboards VALUES('managers','1y',?, 'x', 1)", (json.dumps(strip_detail(BOARD)),))
    srv, u = _serve(tmp_path, lambda: db.connect_read_only(path))
    try:
        board = u + "/api/leaderboard?group=managers&window=1y"
        assert json.loads(get(board)[2])["as_of"] == "x"
        assert json.loads(get(board)[2])["as_of"] == "x"            # served from the cache
        with db.transaction(conn):
            conn.execute("UPDATE leaderboards SET json=?, data_version=2",
                         (json.dumps({**strip_detail(BOARD), "as_of": "y"}),))
        assert json.loads(get(board)[2])["as_of"] == "y"
    finally:
        srv.shutdown()
