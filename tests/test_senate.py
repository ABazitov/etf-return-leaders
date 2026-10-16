from datetime import date
from pathlib import Path

import pytest

from app.sources.errors import ParseRejected
from app.sources.senate import parse_ptr_html, parse_search_json

FIX = Path(__file__).parent / "fixtures" / "senate"
FETTERMAN = "ptr_0541be4f-4f96-4d84-8d6f-d349f218bb2a.html"
WHITEHOUSE = "ptr_6bf3b6f7-9e1b-499a-bd5a-990292ce2e72.html"


def test_search_listing():
    total, rows = parse_search_json((FIX / "search_2026.json").read_text())
    assert len(rows) == 100 and total >= 100
    assert sum(r.kind == "ptr" for r in rows) == 93 and sum(r.kind == "paper" for r in rows) == 7
    assert sum(r.amendment_no > 0 for r in rows) == 17
    f = rows[0]
    assert (f.first, f.last, f.kind, f.filed, f.report_for, f.amendment_no) == (
        "John", "Fetterman", "ptr", date(2026, 10, 7), date(2026, 10, 7), 0)
    assert f.url == "https://efdsearch.senate.gov/search/view/ptr/0541be4f-4f96-4d84-8d6f-d349f218bb2a/"
    boozman = next(r for r in rows if r.last == "Boozman" and r.amendment_no == 1)
    assert (boozman.report_for, boozman.filed) == (date(2025, 12, 8), date(2026, 8, 24))
    paper = next(r for r in rows if r.kind == "paper")
    assert (paper.first, paper.last) == ("RICHARD", "BLUMENTHAL")


def test_ptr_table_fetterman():
    trades = parse_ptr_html((FIX / FETTERMAN).read_text())
    assert len(trades) == 2
    t = trades[0]
    assert (t.number, t.tx_date, t.owner, t.ticker, t.asset_type, t.tx_type, t.amount_text) == (
        2, date(2026, 9, 16), "DC", "--", "Corporate Bond", "Purchase", "$1,001 - $15,000")


def test_ptr_table_whitehouse():
    trades = parse_ptr_html((FIX / WHITEHOUSE).read_text())
    assert len(trades) == 5
    assert (trades[0].number, trades[0].ticker, trades[0].owner, trades[0].tx_type, trades[0].amount_text) == (
        5, "JPM", "SP", "Sale (Partial)", "$15,001 - $50,000")


def test_paper_page_rejected():
    page = next(FIX.glob("paper_*.html")).read_text()
    with pytest.raises(ParseRejected):
        parse_ptr_html(page)


def test_parse_title():
    from app.sources.senate import parse_title
    assert parse_title("Periodic Transaction Report for 12/08/2025 (Amendment 1)") == (date(2025, 12, 8), 1)
    assert parse_title("Periodic Transaction Report for 10/07/2026") == (date(2026, 10, 7), 0)


HDR = ["#", "Transaction Date", "Owner", "Ticker", "Asset Name", "Asset Type", "Type", "Amount", "Comment"]


def _table(header, *rows):
    cells = lambda r, t: "<tr>" + "".join(f"<{t}>{c}</{t}>" for c in r) + "</tr>"
    return "<table>" + cells(header, "th") + "".join(cells(r, "td") for r in rows) + "</table>"


def _row(owner="Self", n="1", d="01/02/2026"):
    return [n, d, owner, "AAPL", "Apple", "Stock", "Purchase", "$1,001 - $15,000", "--"]


def test_site_path():
    from app.sources.senate import _site_path
    assert _site_path("https://efdsearch.senate.gov/search/view/ptr/x/") == "/search/view/ptr/x/"
    assert _site_path("/search/view/ptr/x/") == "/search/view/ptr/x/"
    for bad in ("https://evil.example/x", "//evil.example/x", "search/x", "http://efdsearch.senate.gov/x"):
        with pytest.raises(ValueError):
            _site_path(bad)


def test_owner_mapping():
    page = _table(HDR, _row("Self"), _row("Joint", "2"), _row("Child", "3"), _row("Spouse", "4"))
    assert [t.owner for t in parse_ptr_html(page)] == ["self", "JT", "DC", "SP"]


def test_table_rejections():
    bad = [
        _table(HDR, _row()[:-1]),
        _table(HDR, _row("Cousin")),
        _table(["#", "Date"] + HDR[2:], _row()),
        _table(HDR, _row(n="x")),
        _table(HDR, _row(d="2026-01-02")),
    ]
    for page in bad:
        with pytest.raises(ParseRejected):
            parse_ptr_html(page)


def test_unclosed_tr_flushed():
    page = "<table><tr><th>" + "</th><th>".join(HDR) + "</th><tr>" + "".join(f"<td>{c}</td>" for c in _row()) + "</table>"
    assert len(parse_ptr_html(page)) == 1


def test_malformed_search_rows():
    import json
    good = ["A", "B", "O", '<a href="/search/view/ptr/u/">Periodic Transaction Report for 01/02/2026</a>', "01/02/2026"]
    def mk(row):
        return json.dumps({"recordsTotal": 1, "data": [row]})
    assert parse_search_json(mk(good))[0] == 1
    for row in (good[:3] + ["no link here", "01/02/2026"], good[:4] + ["bad date"], good[:4]):
        with pytest.raises(ParseRejected):
            parse_search_json(mk(row))
    with pytest.raises(ParseRejected):
        parse_search_json("{}")


def test_repeated_hash_numbers_get_unique_positions():
    # real page whose "#" column reads 7,6,5,4,3,2,7: "#" is kept for display, `row` is the table position
    trades = parse_ptr_html((FIX / "ptr_26ae70ce-c44b-4186-b6f3-89b7b9a00b0a.html").read_text())
    assert [t.number for t in trades] == [7, 6, 5, 4, 3, 2, 7]
    assert [t.row for t in trades] == [1, 2, 3, 4, 5, 6, 7]
