"""SenateClient in a real Chromium window against a local stand-in for the Senate site (no network).
Run: .venv/bin/python -m pytest -m e2e tests/e2e/test_senate_client.py"""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.sources import senate

pytestmark = pytest.mark.e2e

# What the site's content network answered on 2026-10-15 (Akamai writes the reference with HTML entities)
DENIED = (b"<HTML><HEAD> <TITLE>Access Denied</TITLE> </HEAD><BODY> <H1>Access Denied</H1> You don't have permission "
          b"to access this server.<P> Reference&#32;&#35;18&#46;c3f93117&#46;1792025931&#46;11aeeb67 </BODY></HTML>")
HOME = b"<input id='agree_statement' type='checkbox' onclick=\"location.href='/search/'\">"


@pytest.fixture
def site(monkeypatch):
    state = {"deny": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if state["deny"]:
                code, body = 403, DENIED
            else:
                code, body = 200, HOME if self.path.startswith("/search/home") else b"<p>search</p>"
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(senate, "BASE", f"http://127.0.0.1:{srv.server_address[1]}")
    yield state
    srv.shutdown()


def _window_state(client):
    s = client._page.context.new_cdp_session(client._page)
    wid = s.send("Browser.getWindowForTarget")["windowId"]
    return s.send("Browser.getWindowBounds", {"windowId": wid})["bounds"]["windowState"]


def test_terms_accepted_in_a_minimized_window(site):
    with senate.SenateClient() as client:
        assert client._page.url.endswith("/search/")
        assert _window_state(client) == "minimized"     # macOS ignores the off-screen position


def test_access_denied_is_reported_at_once(site):
    site["deny"] = True
    t = time.perf_counter()
    with pytest.raises(senate.AccessDenied, match=r"HTTP 403.*reference #18\.c3f93117\.1792025931\.11aeeb67"):
        senate.SenateClient().__enter__()
    assert time.perf_counter() - t < 15                  # not a 30 s wait for a checkbox that never comes
