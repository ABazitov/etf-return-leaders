"""Drives the real page in Chromium. Run: .venv/bin/python -m pytest -m e2e tests/e2e/test_page.py"""
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from app import db, issues
from tests.e2e.fixture_db import EVIL, build

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start(tmp, prepare=None):
    build(tmp / "t.db")
    if prepare:
        prepare(tmp / "t.db")
    (tmp / "config.toml").write_text('sec_contact_email = "e2e@example.org"\n')
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "app.main", "--config", str(tmp / "config.toml"), "--db",
                             str(tmp / "t.db"), "--port", str(port), "--no-jobs", "--no-browser"], cwd=ROOT)
    url = f"http://127.0.0.1:{port}/"
    for _ in range(50):
        try:
            urllib.request.urlopen(url)
            break
        except OSError:
            time.sleep(0.1)
    return proc, url


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    proc, url = _start(tmp_path_factory.mktemp("e2e"))
    yield url
    proc.terminate()


@pytest.fixture
def page(app_url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        p = b.new_page()
        p.goto(app_url)
        p.wait_for_selector("tbody tr.row")
        yield p
        b.close()


def api(app_url, path):
    return json.loads(urllib.request.urlopen(app_url + path.lstrip("/")).read())


def test_numbers_on_screen_equal_api(page, app_url):
    for group, tab in (("managers", "MANAGERS"), ("investors", "13F"), ("congress", "CONGRESS")):
        page.get_by_role("tab", name=tab).click()
        if group == "investors":
            page.locator("#minbook").select_option("0")      # every fixture filer has 4.2% of its book in ETFs
        for win in ("1y", "3y", "5y"):
            page.keyboard.press(win[0])
            page.wait_for_function("n => { const c = document.querySelector('tbody tr.row td:nth-child(2)'); return c && c.textContent === n; }",
                                   arg=f"Alpha {group} {win}")
            b = api(app_url, f"/api/leaderboard?group={group}&window={win}")
            cells = page.locator("tbody tr.row td.ret .main").all_text_contents()
            expect = [("+" if r["total"] >= 0 else "−") + f"{abs(r['total']) * 100:.1f}%" for r in b["ranked"]]
            assert cells == expect, (group, win)
            assert page.locator("th.yr").is_visible() == (win != "1y")


def test_hostile_text_is_shown_as_text(page):
    assert page.locator("tbody").get_by_text(EVIL).count() == 1
    assert page.title() != "pwned"


def test_expand_in_place_with_chart(page):
    first = page.locator("tbody tr.row").first
    first.click()
    detail = page.locator("tbody tr.detail").first
    detail.wait_for()
    detail.locator("svg polyline").first.wait_for()  # the details load asynchronously
    assert detail.locator("svg polyline").count() == 2
    first.press("Enter")
    assert page.locator("tbody tr.detail").count() == 0


def test_search_and_keyboard(page):
    page.keyboard.press("/")
    page.keyboard.type("gamma")
    assert page.locator("tbody tr.row").count() == 1
    page.keyboard.press("Escape")


def test_not_enough_data_and_issues(page):
    page.get_by_text("NOT ENOUGH DATA (1)").click()
    assert page.get_by_text("invested on 15.6% of trading days").is_visible()
    page.get_by_role("button", name="1 issues").click()
    page.get_by_text("1 item was left out or flagged by the data checks.", exact=False).wait_for()
    page.get_by_text("house · scanned").click()                    # one collapsed group per source and kind
    page.get_by_text("Hon. Example (OK04): scanned").wait_for()  # the issues list loads asynchronously
    assert page.get_by_text("Hon. Example (OK04): scanned").is_visible()


def test_issue_kinds_load_in_pages(tmp_path):
    from playwright.sync_api import sync_playwright

    def many(path):
        conn = db.connect(path)
        with db.transaction(conn):
            for i in range(450):
                issues.record(conn, "leaderboard", "x2-mismatch", f"r{i:03}", f"position {i:03} disagrees")

    proc, url = _start(tmp_path, many)
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            p = b.new_page()
            p.goto(url)
            p.wait_for_selector("tbody tr.row")
            p.get_by_role("button", name="451 issues").click()
            group = p.locator("#issues details", has_text="leaderboard · x2-mismatch")
            group.locator("summary").click()
            p.get_by_text("position 199 disagrees").wait_for()
            assert group.locator(".item").count() == 200
            group.get_by_role("button", name="show 200 more of 250").click()
            p.get_by_text("position 399 disagrees").wait_for()
            group.get_by_role("button", name="show 50 more of 50").click()
            p.get_by_text("position 449 disagrees").wait_for()
            assert group.locator(".item").count() == 450 and group.get_by_role("button").count() == 0
            b.close()
    finally:
        proc.terminate()


def test_etf_share_of_the_book(page):
    page.get_by_role("tab", name="13F").click()
    page.locator("#minbook").select_option("0")
    page.wait_for_function("() => document.querySelector('tbody tr.row td:nth-child(2)').textContent === 'Alpha investors 1y'")
    assert "% BOOK" in page.locator("thead").inner_text()
    cell = page.locator("tbody tr.row").first.get_by_text("4.2%", exact=True)
    assert cell.get_attribute("title") == "average over the window: 5.0%"
    page.locator("tbody tr.row").first.click()
    page.get_by_text("4.2% of book").wait_for()                 # the period, read back from its store
    assert page.get_by_text("IAU", exact=True).count() >= 1


def test_book_floor_hides_small_etf_books_by_default(page):
    page.get_by_role("tab", name="13F").click()
    page.get_by_text("3 ranked filers with ETFs under 10% of their book hidden", exact=False).wait_for()
    assert page.locator("tbody tr.row").count() == 0
    page.locator("#minbook").select_option("0")
    assert page.locator("tbody tr.row").count() == 3
    assert "4 quarters · 98.7% priced" in page.locator("tbody tr.row").first.inner_text()


def test_headline_and_plain_words_detail(page):
    page.wait_for_function("() => document.querySelector('#headline').textContent !== ''")
    assert page.locator("#headline").inner_text() == (
        "Fund managers, 1 year: 1 of 3 ranked people beat the S&P 500 (33%). The median was 9.0 points behind. "
        "Of the 1 ranked over 1, 3 and 5 years, 0 beat it in all three.")
    assert page.locator("th", has_text="vs SPX").get_attribute("title").startswith("Percentage points above")
    page.locator("tbody tr.row").first.click()
    page.get_by_text("Return +42.0% against +10.0% for the S&P 500 over the same dates: +32.0 points ahead of it. "
                     "Based on 1 fund. Beat the S&P 500: 1Y yes · 3Y yes · 5Y no.").wait_for()


def test_congress_says_when_the_senate_is_missing(page):
    page.get_by_role("tab", name="CONGRESS").click()
    page.get_by_text("Senate reports are not included (turned off in config.toml)", exact=False).wait_for()
    page.get_by_role("tab", name="MANAGERS").click()
    page.locator("#note").wait_for(state="hidden")


def test_status_bar(page, app_url):
    # The server shows the market open only during a live session with fresh prices (real clock), so the bar
    # must agree with the API rather than with a fixed text.
    market = api(app_url, "/api/status")["market"]
    page.wait_for_function("() => !document.querySelector('#market').textContent.includes('…')")
    assert page.get_by_text("MKT OPEN" if market["open"] else "MKT CLOSED").is_visible()
    sources = page.locator("#sources").inner_text()
    assert sources.count("stale") == 0
    assert "PX" in sources and "SENATE" not in sources        # Senate not enabled in this config


def test_phone_width_has_no_page_scroll(page):
    page.set_viewport_size({"width": 400, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_non_http_source_links_are_not_links(page):
    page.locator("tbody tr.row").nth(2).click()
    page.locator("tbody tr.detail").first.get_by_text("A Fund").wait_for()
    assert page.locator('a[href^="javascript"]').count() == 0
