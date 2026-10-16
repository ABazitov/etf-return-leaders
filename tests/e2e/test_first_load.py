"""First start: no leaderboards yet. The page says so, survives typing, and loads the boards once they exist."""
import sys
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from app import db
from tests.e2e.fixture_db import build
from tests.e2e.test_page import ROOT, free_port

pytestmark = pytest.mark.e2e


@pytest.fixture
def empty_app(tmp_path):
    db.connect(tmp_path / "t.db").close()
    (tmp_path / "config.toml").write_text('sec_contact_email = "e2e@example.org"\n')
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "app.main", "--config", str(tmp_path / "config.toml"), "--db",
                             str(tmp_path / "t.db"), "--port", str(port), "--no-jobs", "--no-browser"], cwd=ROOT)
    url = f"http://127.0.0.1:{port}/"
    for _ in range(50):
        try:
            urllib.request.urlopen(url)
            break
        except OSError:
            time.sleep(0.1)
    yield url, tmp_path / "t.db"
    proc.terminate()


def test_first_load_shows_computing_then_loads_boards(empty_app):
    from playwright.sync_api import sync_playwright
    url, path = empty_app
    errors = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        page = b.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.clock.install()
        page.goto(url)
        page.get_by_text("COMPUTING FIRST LEADERBOARDS…").wait_for()
        page.wait_for_function("() => document.querySelector('#issues-btn').textContent === '0 issues'")  # polled
        page.keyboard.press("/")
        page.keyboard.type("x")                 # render() with no board yet must not throw
        page.keyboard.press("Escape")
        page.keyboard.press("3")                # neither may switching the window
        build(path)                             # the first leaderboards are written (data_version 1)
        page.clock.run_for(61_000)
        page.wait_for_selector("tbody tr.row")
        assert "COMPUTING" not in page.locator("#caption").inner_text()
        b.close()
    assert errors == []
