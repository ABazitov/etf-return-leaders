import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import http as h


class Handler(BaseHTTPRequestHandler):
    calls = {"flaky": 0}

    def do_GET(self):
        if self.path == "/flaky":
            Handler.calls["flaky"] += 1
            if Handler.calls["flaky"] <= 2:
                self.send_response(429); self.end_headers(); return
        if self.path == "/missing":
            self.send_response(404); self.end_headers(); return
        if self.path == "/truncated":
            Handler.calls["truncated"] = Handler.calls.get("truncated", 0) + 1
            self.send_response(200); self.send_header("Content-Length", "100"); self.end_headers()
            self.wfile.write(b"0123456789"); self.wfile.flush()
            self.close_connection = True
            return
        body = self.headers.get("User-Agent", "").encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = self.rfile.read(n)
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(h, "_sleep", slept.append)
    return slept


def test_sends_user_agent(server, sleeps):
    assert h.fetch(server + "/ok", ua="ETF-Return-Leaders/1.0") == b"ETF-Return-Leaders/1.0"


def test_retries_429_with_backoff(server, sleeps):
    Handler.calls["flaky"] = 0
    assert h.fetch(server + "/flaky", ua="x", min_interval=0) == b"x"
    assert Handler.calls["flaky"] == 3
    assert [s for s in sleeps if s >= 1] == [1, 2]


def test_404_raises_without_retry(server, sleeps):
    with pytest.raises(h.HttpError) as e:
        h.fetch(server + "/missing", ua="x", min_interval=0)
    assert e.value.status == 404
    assert [s for s in sleeps if s >= 1] == []


def test_post_body(server, sleeps):
    assert h.fetch(server + "/echo", ua="x", data=b"hello", min_interval=0) == b"hello"


# ---------- final review item 7: errors while reading the body are retried and typed ----------

def test_truncated_body_is_retried_then_raised_as_http_error(server, sleeps):
    Handler.calls["truncated"] = 0
    with pytest.raises(h.HttpError) as e:
        h.fetch(server + "/truncated", ua="x", min_interval=0, attempts=3)
    assert e.value.status is None and "IncompleteRead" in str(e.value)
    assert Handler.calls["truncated"] == 3
    assert [s for s in sleeps if s >= 1] == [1, 2]


@pytest.mark.parametrize("exc", [lambda: __import__("ssl").SSLError("bad record mac"),
                                 lambda: OSError("connection reset during read"),
                                 lambda: __import__("http.client").client.RemoteDisconnected("closed")])
def test_read_errors_are_retried(monkeypatch, sleeps, exc):
    calls = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            calls.append(1)
            if len(calls) < 2:
                raise exc()
            return b"ok"
    monkeypatch.setattr(h.urllib.request, "urlopen", lambda req, timeout: Resp())
    assert h.fetch("http://example.invalid/x", ua="x", min_interval=0) == b"ok"
    assert len(calls) == 2
