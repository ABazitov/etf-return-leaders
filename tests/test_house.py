from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from app.sources.errors import ParseRejected
from app.sources.house import extract_text, parse_index_xml, parse_pdf, parse_text, pdf_url

FIX = Path(__file__).parent / "fixtures" / "house"


def test_index():
    rows = parse_index_xml((FIX / "2026FD.xml").read_bytes())
    assert len(rows) == 1746
    assert sum(1 for r in rows if r.filing_type == "P") == 415
    gill = next(r for r in rows if r.doc_id == "20035410")
    assert (gill.last, gill.first, gill.state_district, gill.filed, gill.year) == (
        "Gill", "Brandon", "TX26", date(2026, 9, 9), 2026)
    blank = [r for r in rows if r.filed is None]
    assert len(blank) == 37 and all(r.filing_type == "W" for r in blank)


def test_pdf_url():
    assert pdf_url(2026, "20035410") == "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035410.pdf"


def test_single_etf_purchase():
    r = parse_pdf(FIX / "20035410.pdf")
    assert (r.name, r.status, r.state_district, r.filing_id) == ("Hon. Brandon Gill", "Member", "TX26", "20035410")
    [t] = r.trades
    assert (t.owner, t.asset_text, t.asset_code, t.tx_type, t.tx_date, t.amount_text) == (
        "self", "RSP ETF", "OT", "P", date(2026, 8, 13), "$50,001 - $100,000")


def test_joint_account_with_option_trade():
    r = parse_pdf(FIX / "20034305.pdf")
    assert len(r.trades) == 18 and {t.owner for t in r.trades} == {"JT"}
    opt = [t for t in r.trades if t.asset_code == "OP"]
    assert [(t.asset_text, t.amount_text) for t in opt] == [
        ("Microsoft Corporation - Common Stock (MSFT)", "$50,001 - $100,000"),
        ("Microsoft Corporation - Common Stock (MSFT)", "$500,001 - $1,000,000")]


@pytest.mark.parametrize("doc,n", [("20034998", 31), ("20035191", 15), ("20035408", 78),
                                   ("20034106", 33), ("20034247", 6), ("20033744", 1)])
def test_trade_counts(doc, n):
    assert len(parse_pdf(FIX / f"{doc}.pdf").trades) == n


def test_spouse_trades():
    assert Counter(t.owner for t in parse_pdf(FIX / "20034998.pdf").trades) == {"SP": 31}


def test_record_split_across_page_break():
    r = parse_pdf(FIX / "20035408.pdf")
    t = next(t for t in r.trades if t.tx_date == date(2026, 8, 5) and "CHRW" in t.asset_text)
    assert (t.owner, t.asset_text, t.tx_type, t.amount_text) == (
        "DC", "C.H. Robinson Worldwide, Inc. - Common Stock (CHRW)", "P", "$1,001 - $15,000")


def test_open_ended_and_exact_amounts():
    assert "Spouse/DC Over $1,000,000" in {t.amount_text for t in parse_pdf(FIX / "20035191.pdf").trades}
    assert "$318.74" in {t.amount_text for t in parse_pdf(FIX / "20034247.pdf").trades}


def test_exchange_transaction():
    [t] = parse_pdf(FIX / "20033744.pdf").trades
    assert (t.tx_type, t.asset_text) == ("E", "VERSANT MEDIA GROUP INC CL A (VSNT)")


def test_scanned_report_rejected():
    with pytest.raises(ParseRejected, match="scanned"):
        parse_pdf(FIX / "9116249.pdf")


def test_unreadable_record_rejects_whole_report():
    text = extract_text(FIX / "20034305.pdf").replace("P 03/25/2026 04/07/2026", "P 03/25/2026 ??/07/2026", 1)
    with pytest.raises(ParseRejected, match="asset codes|unparsed text"):
        parse_text(text)


@pytest.mark.parametrize("doc", ["20029134", "20030541", "20033564"])
def test_2025_reports_parse(doc):
    assert parse_pdf(FIX / f"{doc}.pdf").trades


def test_amount_split_across_page_break():
    hog = [t for t in parse_pdf(FIX / "20029134.pdf").trades if "(HOG)" in t.asset_text]
    assert ("P", date(2025, 3, 4), "$50,001 - $100,000") in [(t.tx_type, t.tx_date, t.amount_text) for t in hog]


def test_stray_line_before_detail_rejects_report():
    from app.sources.house import _is_detail_line
    lines = extract_text(FIX / "20034305.pdf").split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip() == "$200?")
    i = next(i for i in range(start, len(lines)) if _is_detail_line(lines[i]))
    lines.insert(i, "Some stray comment")
    with pytest.raises(ParseRejected, match="unparsed text"):
        parse_text("\n".join(lines))


def test_stray_line_merged_into_asset_rejects_report():
    lines = extract_text(FIX / "20034998.pdf").split("\n")
    i = next(i for i, l in enumerate(lines) if l.startswith("SP Duke Energy"))
    lines.insert(i, "wrapped comment continuation")
    with pytest.raises(ParseRejected, match="unparsed text"):
        parse_text("\n".join(lines))


def test_comment_prose_before_owner_record_is_dropped():
    trades = parse_pdf(FIX / "20026590.pdf").trades
    t = next(t for t in trades if t.asset_text == "NVIDIA Corporation - Common Stock (NVDA)"
             and t.tx_type == "S (partial)")
    assert (t.owner, t.asset_code, t.tx_date, t.amount_text) == (
        "SP", "ST", date(2024, 12, 31), "$1,000,001 - $5,000,000")
    assert not any("12/20/24" in t.asset_text for t in trades)


def test_comment_prose_before_owner_record_kroger():
    trades = parse_pdf(FIX / "20030207.pdf").trades
    [t] = [t for t in trades if "Kroger" in t.asset_text]
    assert (t.owner, t.asset_text, t.tx_type, t.tx_date, t.amount_text) == (
        "SP", "Kroger Company (KR)", "S", date(2025, 5, 9), "$250,001 - $500,000")


def test_leading_filer_id_column_is_skipped():
    trades = parse_pdf(FIX / "20030875.pdf").trades
    [t] = [t for t in trades if "Alibaba" in t.asset_text]
    assert (t.owner, t.asset_text, t.tx_type, t.tx_date, t.amount_text) == (
        "SP", "Alibaba Group Holding Limited American Depositary Shares each representing eight Ordinary share (BABA)",
        "S", date(2023, 12, 29), "$100,001 - $250,000")


# ---------- C9: filing status of each row ----------

def test_filing_status_new_layout():
    r = parse_pdf(FIX / "20021325.pdf")
    assert [(t.owner, t.asset_text, t.asset_code, t.amount_text, t.filing_status) for t in r.trades] == [
        ("SP", "Uber Technologies, Inc. (UBER)", "ST", "$1,001 - $15,000", "New"),
        ("SP", "VMWare Inc Note", "CS", "$15,001 - $50,000", "Deleted")]
    assert Counter(t.filing_status for t in parse_pdf(FIX / "20030875.pdf").trades)["Amended"] == 2
    assert {t.filing_status for t in parse_pdf(FIX / "20035408.pdf").trades} == {"New"}


def test_unknown_filing_status_rejects_report():
    text = extract_text(FIX / "20021325.pdf").replace(": New", ": Pending", 1)
    with pytest.raises(ParseRejected, match="filing status"):
        parse_text(text)


def test_comment_wrapped_up_to_the_table_end():
    [t] = parse_pdf(FIX / "20021740.pdf").trades          # 2022: "... below the\nrequired threshold."
    assert (t.owner, t.asset_text, t.asset_code, t.tx_type, t.tx_date, t.amount_text) == (
        "self", "Apple Inc. (AAPL)", "ST", "P", date(2022, 8, 26), "$1,001 - $15,000")


def test_subholding_text_wrapped_onto_its_own_line():
    # "SUBHOLDING OF: ... > Randall Robert" / "Kaplan Alaska LLC", and once "... Alaska" / page header / "LLC"
    trades = parse_pdf(FIX / "20021754.pdf").trades
    assert len(trades) == 17
    funds = [(t.owner, t.asset_text, t.asset_code, t.tx_type, t.tx_date, t.amount_text)
             for t in trades if "Seven Bridges" in t.asset_text]
    assert funds == [
        ("SP", "Seven Bridges Multi-Strategy Fund LP", "OT", "P", date(2022, 10, 1), "$100,001 - $250,000"),
        ("SP", "Seven Bridges Multi-Strategy Fund Series", "OT", "P", date(2022, 10, 1), "$15,001 - $50,000"),
        ("self", "Seven Bridges Multi-Strategy Fund Series", "OT", "P", date(2022, 10, 1), "$100,001 - $250,000"),
        ("self", "Seven Bridges Multi-Strategy Fund Series", "OT", "P", date(2022, 10, 1), "$15,001 - $50,000")]
    assert not any("Kaplan" in t.asset_text or t.asset_text.startswith("LLC") for t in trades)
    assert trades[-1].asset_text == "Tesla, Inc. (TSLA)"


def test_wrapped_comment_text_still_rejects_the_report():
    # Its wrapped comments would otherwise run into the next record, which has no owner prefix, and be read as part
    # of that asset ("company-specific holdings into passive ETFs ... Bank of America Corporation Common Stock").
    with pytest.raises(ParseRejected, match="unparsed text"):
        parse_pdf(FIX / "20027982.pdf")


@pytest.mark.parametrize("extra", ["Some Fund [MF]", "10/01/2022 10/01/2022", "$1,001 - $15,000"])
def test_wrapped_subholding_text_that_looks_like_a_transaction_rejects(extra):
    text = extract_text(FIX / "20021754.pdf").replace("\nKaplan Alaska LLC\n", f"\nKaplan Alaska LLC {extra}\n")
    with pytest.raises(ParseRejected):
        parse_text(text)


def test_text_with_asset_code_before_table_end_still_rejects():
    text = extract_text(FIX / "20021740.pdf").replace("required threshold.", "required threshold.\nSome Fund [MF]")
    with pytest.raises(ParseRejected):
        parse_text(text)


@pytest.mark.parametrize("extra", ["08/26/2022 08/26/2022", "$1,001 - $15,000", "Spouse/DC Over $1,000,000"])
def test_trailing_comment_text_that_looks_like_a_transaction_rejects(extra):
    text = extract_text(FIX / "20021740.pdf").replace("required threshold.", f"required threshold. {extra}")
    with pytest.raises(ParseRejected):
        parse_text(text)
