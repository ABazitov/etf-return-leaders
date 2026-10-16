"""Senate eFD periodic transaction reports (spec C1, C2, C9). The site rejects plain HTTP clients,
so fetching runs inside Chromium via Playwright after ticking the terms checkbox (user accepted at setup)."""
from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser

from app.sources.errors import ParseRejected

BASE = "https://efdsearch.senate.gov"
HEADER = ["#", "Transaction Date", "Owner", "Ticker", "Asset Name", "Asset Type", "Type", "Amount", "Comment"]
OWNER = {"Self": "self", "Spouse": "SP", "Joint": "JT", "Child": "DC"}
_TITLE = re.compile(r"for (\d{2}/\d{2}/\d{4})(?: \(Amendment (\d+)\))?")


@dataclass(frozen=True)
class SenateListing:
    first: str
    last: str
    office: str
    url: str
    kind: str
    title: str
    filed: date
    report_for: date | None
    amendment_no: int


@dataclass(frozen=True)
class SenateTrade:
    row: int
    tx_date: date
    owner: str
    ticker: str
    asset_name: str
    asset_type: str
    tx_type: str
    amount_text: str
    comment: str
    number: int = 0          # the table's "#" column, display only (it can repeat); `row` is the 1-based position


def _mdy(s: str) -> date:
    return datetime.strptime(s.strip(), "%m/%d/%Y").date()


def parse_title(title: str) -> tuple[date | None, int]:
    """'Periodic Transaction Report for 12/08/2025 (Amendment 1)' -> (2025-12-08, 1)."""
    m = _TITLE.search(title)
    return (_mdy(m[1]) if m else None), (int(m[2]) if m and m[2] else 0)


def parse_search_json(text: str) -> tuple[int, list[SenateListing]]:
    try:
        payload = json.loads(text)
        out = []
        for first, last, office, link, filed in payload["data"]:
            href = re.search(r'href="([^"]+)"', link)[1]
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", link)).strip()
            report_for, amendment_no = parse_title(title)
            out.append(SenateListing(first.strip(), last.strip(), office.strip(), BASE + href,
                                     "paper" if "/paper/" in href else "ptr", title, _mdy(filed),
                                     report_for, amendment_no))
        return int(payload["recordsTotal"]), out
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        raise ParseRejected(f"unexpected Senate search JSON: {e!r}") from e


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self.in_table = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table += 1
        elif tag == "tr" and self.in_table:
            if self._row is not None:
                self.rows.append(self._row)
            self._cell = None
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", html_lib.unescape("".join(self._cell))).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            if self._row is not None:
                self.rows.append(self._row)
                self._row = None
            self._cell = None
            self.in_table -= 1

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_ptr_html(page: str) -> list[SenateTrade]:
    p = _Tables()
    p.feed(page)
    if not p.rows or p.rows[0] != HEADER:
        raise ParseRejected("no transaction table with the expected columns (paper or changed layout)")
    out = []
    for pos, r in enumerate(p.rows[1:], 1):
        if len(r) != len(HEADER):
            raise ParseRejected(f"row with {len(r)} cells")
        if r[2] not in OWNER:
            raise ParseRejected(f"unknown owner {r[2]!r}")
        try:
            out.append(SenateTrade(pos, _mdy(r[1]), OWNER[r[2]], r[3], r[4], r[5], r[6], r[7], r[8], int(r[0])))
        except ValueError as e:
            raise ParseRejected(f"bad transaction row {r!r}: {e}") from e
    return out


def _site_path(url: str) -> str:
    """Same-site path only: BASE + '/...' or a bare '/...' (not '//host')."""
    path = url.removeprefix(BASE) if url.startswith(BASE + "/") else url
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"not a Senate site path: {url!r}")
    return path


class AccessDenied(Exception):
    """The site's content network refused the browser outright ("Access Denied", HTTP 403)."""


class SenateClient:
    """The Senate site refuses headless browsers (HTTP 403, checked 2026-10-09), so the browser
    runs headed, placed off-screen and minimized (macOS ignores the position) unless visible=True."""

    def __init__(self, visible: bool = False):
        self.visible = visible

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            args = [] if self.visible else ["--window-position=-2400,-2400", "--window-size=1000,800"]
            self._browser = self._pw.chromium.launch(headless=False, args=args)
            self._page = self._browser.new_page()
            if not self.visible:
                s = self._page.context.new_cdp_session(self._page)
                window = s.send("Browser.getWindowForTarget")["windowId"]
                s.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "minimized"}})
            resp = self._page.goto(BASE + "/search/home/")
            if resp is not None and resp.status == 403:
                ref = re.search(r"Reference #(\S+)", self._page.inner_text("body"))
                raise AccessDenied(f"efdsearch.senate.gov refused the browser (HTTP 403 Access Denied"
                                   + (f", reference #{ref[1]})" if ref else ")"))
            self._page.locator("#agree_statement").click()
            self._page.wait_for_url("**/search/")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        browser = getattr(self, "_browser", None)
        pw = getattr(self, "_pw", None)
        self._browser = self._pw = None
        try:
            if browser is not None:
                browser.close()
        finally:
            if pw is not None:
                pw.stop()

    def search(self, submitted_from: date) -> list[SenateListing]:
        out: list[SenateListing] = []
        start = 0
        while True:
            text = self._page.evaluate(
                """async ([start, from]) => {
                  const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1];
                  const body = new URLSearchParams({start: String(start), length: '100', report_types: '[11]',
                    filer_types: '[]', submitted_start_date: from + ' 00:00:00', submitted_end_date: '',
                    candidate_state: '', senator_state: '', office_id: '', first_name: '', last_name: '',
                    csrfmiddlewaretoken: csrf});
                  const r = await fetch('/search/report/data/', {method: 'POST', body,
                    headers: {'X-CSRFToken': csrf, 'Content-Type': 'application/x-www-form-urlencoded'}});
                  if (!r.ok) throw new Error('search HTTP ' + r.status);
                  return await r.text();
                }""", [start, submitted_from.strftime("%m/%d/%Y")])
            total, rows = parse_search_json(text)
            out.extend(rows)
            start += len(rows)
            if start >= total or not rows:
                return out

    def page(self, url: str) -> str:
        path = _site_path(url)
        return self._page.evaluate(
            "async (p) => { const r = await fetch(p); if (!r.ok) throw new Error('HTTP ' + r.status); return await r.text(); }",
            path)
