"""House PTRs filed before 2022 use an older PDF layout, read with pypdf's layout mode (spec C1)."""
import re
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from app.sources.errors import ParseRejected
from app.sources.house import extract_layout_text, parse_layout_text, parse_pdf

OLD = Path(__file__).parent / "fixtures" / "house" / "old"


def rec(t):
    return (t.owner, t.asset_text, t.asset_code, t.tx_type, t.tx_date, t.notified, t.amount_text)


def test_2018_single_wrapped_record_and_header():
    r = parse_pdf(OLD / "2018-20008973.pdf")
    assert (r.name, r.status, r.state_district, r.filing_id) == ("Hon. James R. Langevin", "Member", "RI02", "20008973")
    assert [rec(t) for t in r.trades] == [(
        "self", "alibaba group Holding Limited american Depositary shares each representing one Ordinary share (BABA)",
        "", "S", date(2018, 2, 14), date(2018, 2, 14), "$100,001 - $250,000")]
    assert r.trades[0].row == 1 and r.trades[0].description == ""


def test_2021_code_bearing_layout_with_wrapped_amount():
    r = parse_pdf(OLD / "2021-20018021.pdf")
    assert (r.name, r.state_district, r.filing_id) == ("Hon. Richard W. Allen", "GA12", "20018021")
    assert [rec(t) for t in r.trades] == [(
        "SP", "Dominion Energy, Inc. (D)", "ST", "S", date(2020, 12, 15), date(2020, 12, 18), "$15,001 - $50,000")]


def test_2015_multi_row_spouse_wrapped_names_and_one_digit_days():
    r = parse_pdf(OLD / "2015-20002402.pdf")
    assert r.state_district == "IL10"
    assert [rec(t) for t in r.trades] == [
        ("SP", "google Inc. - Class a (GOOGL)", "", "S", date(2014, 12, 18), date(2015, 1, 2), "$50,001 - $100,000"),
        ("SP", "google Inc. - Class C Capital Stock (GOOG)", "", "S", date(2014, 12, 18), date(2015, 1, 2),
         "$50,001 - $100,000"),
        ("SP", "Home Depot, Inc. (HD)", "", "P", date(2014, 12, 18), date(2015, 1, 2), "$100,001 - $250,000"),
        ("SP", "Huntington Ingalls INDS INC 7.12500% 3/15/2021 SR NT", "", "P", date(2014, 12, 10), date(2015, 1, 12),
         "$1,001 - $15,000"),
        ("SP", "Stratasys, ltd. (SSYS)", "", "S", date(2014, 12, 3), date(2014, 1, 2), "$50,001 - $100,000"),
        ("SP", "Telephone and Data Systems, Inc. 5.875% Senior Notes due 2061 (TDA)", "", "S", date(2014, 12, 18),
         date(2015, 1, 2), "$1,001 - $15,000"),
        ("SP", "Zions Bancorp 6.3000% Sersg", "", "P", date(2014, 12, 10), date(2015, 1, 2), "$1,001 - $15,000")]
    assert [t.row for t in r.trades] == [1, 2, 3, 4, 5, 6, 7]
    assert all(t.description == "" for t in r.trades)       # "Purchase" / "Sale" are not option text


def test_2014_capitalised_header_variant_owners_and_page_break():
    r = parse_pdf(OLD / "2014-20000037.pdf")
    assert (r.name, r.status, r.state_district) == ("Mr. Rubén Hinojosa", "Member", "TX15")
    assert len(r.trades) == 14
    assert Counter(t.owner for t in r.trades) == {"SP": 8, "DC": 6}
    assert rec(r.trades[0]) == ("SP", "amerco (UHAL)", "", "S", date(2013, 12, 20), date(2013, 12, 20),
                                "$1,001 - $15,000")
    assert rec(r.trades[-1]) == (
        "SP", "susser Petroleum Partners lP Common units representing limited partner interests (SUSP)", "", "S",
        date(2013, 12, 20), date(2013, 12, 20), "$15,001 - $50,000")
    assert [t.asset_text for t in r.trades if "Roadrunner" in t.asset_text] == [
        "Roadrunner Transportation systems, Inc (RRTS)"] * 2


def test_2014_joint_owner_subholdings_and_option_descriptions():
    r = parse_pdf(OLD / "2014-20000320.pdf")
    assert (r.name, r.state_district, r.filing_id) == ("Mr. Richard L. Hanna", "NY22", "20000320")
    assert len(r.trades) == 15
    assert rec(r.trades[0]) == ("JT", "Coach, Inc. (COH)", "", "S", date(2014, 1, 27), date(2014, 2, 5),
                                "$1,001 - $15,000")
    assert rec(r.trades[1])[0:2] == ("self", "CSX Corporation (CSX)")
    gap = r.trades[3]
    assert (gap.asset_text, gap.tx_type, gap.description) == ("gap, Inc. (GPS)", "S", "CaLL 2/22/2014 $39 (100SHS)")
    ups = next(t for t in r.trades if "(UPS)" in t.asset_text)
    assert ups.description == "PUT 2/22/2014 $97.50 (100SHS)"
    spy = next(t for t in r.trades if "(SPY)" in t.asset_text)
    assert (spy.asset_text, spy.tx_type, spy.amount_text, spy.description) == (
        "SPdR S&P 500 (SPY)", "P", "$15,001 - $50,000", "")
    assert sum(1 for t in r.trades if t.description) == 4   # two calls, two puts; "REINVESTEd dIVIdENd" is dropped


def test_2014_exchange_with_wrapped_description():
    [t] = parse_pdf(OLD / "2014-20000868.pdf").trades
    assert rec(t) == ("self", "AbbVie Inc. (ABBV)", "", "E", date(2013, 1, 1), date(2014, 5, 1), "$1,001 - $15,000")
    assert t.description == ""


def test_2014_long_multi_page_report():
    r = parse_pdf(OLD / "2014-20002231.pdf")
    assert r.state_district == "MA05" and len(r.trades) == 230
    assert rec(r.trades[0]) == ("self", "3M Company (MMM)", "", "S", date(2014, 4, 9), date(2014, 11, 20),
                                "$1,001 - $15,000")
    acn = [t for t in r.trades if t.asset_text.endswith("(ACN)")]
    assert [t.asset_text for t in acn] == ["accenture plc. Class a ordinary Shares (ACN)"] * 2
    altria = [t.tx_date for t in r.trades if t.asset_text == "altria group, Inc. (MO)"]
    assert date(2014, 11, 13) in altria                     # its detail lines follow a page header


def test_2016_description_continuation_line():
    [t] = parse_pdf(OLD / "2016-20005556.pdf").trades
    assert rec(t) == ("self", "METROPOLITAN TRANSN AUTH NY REV TRANSN SER A", "", "P", date(2016, 6, 29),
                      date(2016, 6, 30), "$15,001 - $50,000")


def test_2017_open_ended_spouse_amount_without_codes():
    r = parse_pdf(OLD / "2017-20007467.pdf")
    assert [rec(t) for t in r.trades][3:] == [
        ("SP", "Sacramento, CA", "", "P", date(2017, 5, 3), date(2017, 5, 31), "Spouse/DC Over $1,000,000"),
        ("self", "Winward Academy LLC", "", "P", date(2016, 12, 7), date(2016, 12, 31), "$15,001 - $50,000")]
    assert len(r.trades) == 5


def test_2019_option_code_and_call_descriptions():
    r = parse_pdf(OLD / "2019-20011457.pdf")
    assert [(t.asset_code, t.tx_type, t.tx_date, t.amount_text, t.description) for t in r.trades] == [
        ("OP", "S", date(2019, 4, 1), "$1,001 - $15,000", "Call Option, $177.50, Exp. 4/5/19"),
        ("OP", "S", date(2019, 4, 5), "$1,001 - $15,000", "Call Option, $180, Exp. 4/5/19"),
        ("OP", "S", date(2019, 4, 1), "$1,001 - $15,000", "Call Option, $292.50, Exp. 4/5/19")]
    assert r.trades[0].asset_text == ("alibaba group Holding Limited american Depositary shares each representing "
                                      "one Ordinary share (BABA)")
    assert r.trades[2].asset_text == "Tesla, Inc. (TSLA)"


def test_2019_lower_case_partial_sale_and_code():
    r = parse_pdf(OLD / "2019-20011692.pdf")
    assert [rec(t) for t in r.trades] == [
        ("self", "united states Treasury Notes", "GS", "P", date(2019, 2, 28), date(2019, 5, 15), "$100,001 - $250,000"),
        ("self", "united states Treasury Notes", "GS", "S (partial)", date(2019, 4, 1), date(2019, 5, 15),
         "$15,001 - $50,000"),
        ("self", "united states Treasury Notes", "GS", "S (partial)", date(2019, 4, 24), date(2019, 5, 15),
         "$15,001 - $50,000")]


def test_2020_spouse_over_amount_split_over_two_lines():
    r = parse_pdf(OLD / "2020-20015024.pdf")
    assert len(r.trades) == 7 and r.state_district == "CA52"
    assert rec(r.trades[2]) == ("SP", "Ca EDuC FaC auTH REV", "GS", "S", date(2020, 1, 27), date(2020, 1, 31),
                                "Spouse/DC Over $1,000,000")
    assert rec(r.trades[3]) == ("SP", "San Francisco County Ca EXCISE TaX", "GS", "S", date(2020, 1, 27),
                                date(2020, 1, 31), "$500,001 - $1,000,000")
    assert r.trades[1].owner == "self"


def test_2020_exact_amounts():
    r = parse_pdf(OLD / "2020-20016958.pdf")
    assert [t.amount_text for t in r.trades] == ["$501.24", "$501.24", "$1,001 - $15,000", "$505.91",
                                                 "$1,001 - $15,000", "$1,001 - $15,000", "$1,001 - $15,000"]
    assert rec(r.trades[0])[:5] == ("SP", "Sunrun Inc. (RUN)", "ST", "S (partial)", date(2020, 6, 23))
    assert rec(r.trades[-1]) == ("SP", "Vivint Solar, Inc. (VSLR)", "ST", "S (partial)", date(2020, 7, 7),
                                 date(2020, 7, 14), "$1,001 - $15,000")


def test_2020_asset_name_continued_after_page_header():
    r = parse_pdf(OLD / "2020-20017475.pdf")
    assert len(r.trades) == 19
    usmc = [t for t in r.trades if "Mega-Cap" in t.asset_text]
    assert [rec(t) for t in usmc] == [
        ("self", "Principal U.S. Mega-Cap ETF (USMC)", "ST", "P", date(2020, 9, 21), date(2020, 9, 25),
         "$1,001 - $15,000"),
        ("self", "Principal U.S. Mega-Cap ETF (USMC)", "ST", "S (partial)", date(2020, 9, 8), date(2020, 9, 11),
         "$15,001 - $50,000")]
    nova = next(t for t in r.trades if t.asset_text.startswith("bANK NOVA"))
    assert (nova.asset_text, nova.asset_code) == (
        "bANK NOVA SCOTIA 0% 1Y: LKd EURO STOXX 50 INdEX dUE 09/24/2020 CPN 0.000% dUE 09/24/20", "OT")
    assert next(t for t in r.trades if "Alphabet" in t.asset_text).asset_text == "Alphabet Inc. - Class A (GOOGL)"


# ---------- layout variants found by the stress run over all cached reports ----------

def test_2014_wrapped_subholding_lines():
    r = parse_pdf(OLD / "2014-20001019.pdf")
    assert [rec(t) for t in r.trades] == [
        ("JT", "QR Energy, LP Common Units representing Limited Partner Interests (QRE)", "", "S", date(2014, 5, 28),
         date(2014, 5, 28), "$50,001 - $100,000"),
        ("self", "QR Energy, LP Common Units representing Limited Partner Interests (QRE)", "", "S", date(2014, 5, 23),
         date(2014, 5, 23), "$15,001 - $50,000"),
        ("JT", "QR ENERGY, LP SER B SR LIEN", "", "P", date(2014, 5, 28), date(2014, 5, 28), "$100,001 - $250,000")]


def test_2014_transaction_id_column_on_amended_rows():
    r = parse_pdf(OLD / "2014-20001276.pdf")
    assert len(r.trades) == 19
    tph = [rec(t) for t in r.trades if t.asset_text.startswith("TPH Partners")]
    assert tph == [
        ("self", "TPH Partners lP", "", "P", date(2014, 5, 27), date(2014, 6, 11), "$15,001 - $50,000"),
        ("self", "TPH Partners lP II", "", "P", date(2014, 5, 27), date(2014, 6, 11), "$1,001 - $15,000")]
    assert rec(r.trades[-1]) == ("self", "Williams Companies, Inc. (WMB)", "", "P", date(2014, 6, 23),
                                 date(2014, 7, 7), "$50,001 - $100,000")


def test_2015_lower_case_spouse_over_amount_is_normalised():
    r = parse_pdf(OLD / "2015-20002353.pdf")
    assert [rec(t) for t in r.trades] == [
        ("self", "Henderson Enterprises Inc.", "", "S", date(2014, 12, 31), date(2014, 12, 31),
         "Spouse/DC Over $1,000,000"),
        ("self", "Sacramento County Ca Bonds", "", "S", date(2014, 12, 1), date(2014, 12, 31), "$250,001 - $500,000")]


def test_2016_description_continued_below_a_page_header():
    r = parse_pdf(OLD / "2016-20005456.pdf")
    assert len(r.trades) == 13 and r.state_district == "TX11"
    wells = [t for t in r.trades if t.asset_text.startswith("WELLS FARGO")]
    assert [t.asset_text for t in wells] == [
        "WELLS FARGO & COMPANY JR SUBORDNTD SER K GLB VAR% PERPETUAL CALLABLE-MAY AFFECT YIEL"] * 3
    assert all(t.description == "" for t in r.trades)   # "CALLABLE" is not an option word


# ---------- strictness (C1): anything unaccounted for rejects the whole report ----------

def _lines(name):
    return extract_layout_text(OLD / name).split("\n")


def test_layout_text_round_trip():
    assert len(parse_layout_text("\n".join(_lines("2015-20002402.pdf"))).trades) == 7


def test_corrupted_record_line_rejects_report():
    text = "\n".join(_lines("2015-20002402.pdf")).replace("12/18/2014    01/2/2015", "12/18/2014    O1/2/2015", 1)
    with pytest.raises(ParseRejected, match="dates"):
        parse_layout_text(text)


def test_corrupted_type_rejects_report():
    lines = _lines("2015-20002402.pdf")
    i = next(i for i, l in enumerate(lines) if "Home Depot" in l)
    lines[i] = lines[i].replace("  P  ", "  X  ", 1)
    with pytest.raises(ParseRejected, match="type"):
        parse_layout_text("\n".join(lines))


def test_stray_line_after_filing_status_rejects_report():
    lines = _lines("2015-20002402.pdf")
    i = next(i for i, l in enumerate(lines) if "FIlINg STaTuS" in l)
    lines.insert(i + 1, lines[i].replace("FIlINg STaTuS: New", "some stray words"))
    with pytest.raises(ParseRejected, match="unparsed"):
        parse_layout_text("\n".join(lines))


def test_stray_text_in_amount_column_rejects_report():
    lines = _lines("2018-20008973.pdf")
    i = next(i for i, l in enumerate(lines) if "american Depositary" in l)
    lines[i] = lines[i] + " " * (lines[i - 1].index("$100,001") - len(lines[i])) + "$5"
    with pytest.raises(ParseRejected, match="unparsed"):
        parse_layout_text("\n".join(lines))


def test_extra_date_pair_outside_records_rejects_report():
    lines = _lines("2016-20005556.pdf")
    i = next(i for i, l in enumerate(lines) if "CUSIP" in l)
    lines[i] += " 01/02/2016 01/03/2016"
    with pytest.raises(ParseRejected, match="date pairs"):
        parse_layout_text("\n".join(lines))


def test_unknown_owner_rejects_report():
    lines = _lines("2021-20018021.pdf")
    i = next(i for i, l in enumerate(lines) if "Dominion" in l)
    lines[i] = lines[i].replace("SP", "XX", 1)
    with pytest.raises(ParseRejected, match="owner"):
        parse_layout_text("\n".join(lines))


def test_text_without_transaction_table_is_rejected():
    with pytest.raises(ParseRejected):
        parse_layout_text("name: Hon. A B\nStatus: Member\nState/District: XX01\nFiling ID #1\n")


def test_new_layout_still_uses_the_new_parser():
    r = parse_pdf(OLD.parent / "20035410.pdf")
    assert r.trades[0].asset_code == "OT" and r.trades[0].description == ""


def test_scanned_report_still_rejected_as_scanned():
    with pytest.raises(ParseRejected, match="^scanned$"):
        parse_pdf(OLD.parent / "9116249.pdf")


# ---------- C9: filing status of each row ----------

def test_filing_status_old_layout():
    assert [(t.asset_text, t.filing_status) for t in parse_pdf(OLD / "2014-20001268.pdf").trades] == [
        ("Brookfield Global listed Infrastructure Income Fund Closed End Fund (INF)", "Deleted"),
        ("Oppenheimer Holdings, Inc. Class a Common stock (OPY)", "Deleted")]
    r = parse_pdf(OLD / "2014-20001276.pdf")
    assert Counter(t.filing_status for t in r.trades) == {"New": 17, "Amended": 2}
    assert {t.asset_text for t in r.trades if t.filing_status == "Amended"} == {"TPH Partners lP", "TPH Partners lP II"}


def test_unknown_filing_status_rejects_old_layout_report():
    text = "\n".join(_lines("2015-20002402.pdf")).replace("FIlINg STaTuS: New", "FIlINg STaTuS: Pending", 1)
    with pytest.raises(ParseRejected, match="filing status"):
        parse_layout_text(text)


# ---------- review fixes ----------

def _status_lines(lines):
    return [i for i, l in enumerate(lines) if re.search(r"filing\s+status\s*:", l, re.I)]


def test_stray_left_margin_line_before_a_filing_status_rejects():
    lines = _lines("2014-20001268.pdf")                 # both rows are "Deleted"
    lines.insert(_status_lines(lines)[-1], "stray")     # would end the table before the second row's status
    with pytest.raises(ParseRejected, match="filing status"):
        parse_layout_text("\n".join(lines))


def test_detail_line_shifted_right_into_the_asset_rejects():
    lines = _lines("2018-20008973.pdf")
    i = _status_lines(lines)[0]
    while not lines[i - 1].strip():                      # glue it to the asset lines above
        del lines[i - 1]
        i -= 1
    lines[i] = "    " + lines[i]
    with pytest.raises(ParseRejected, match="detail"):
        parse_layout_text("\n".join(lines))


def test_filing_status_count_must_equal_record_count():
    text = "\n".join(_lines("2015-20002402.pdf")).replace("Digitally Signed:", "FILING STATUS: New\nDigitally Signed:")
    with pytest.raises(ParseRejected, match="filing status"):
        parse_layout_text(text)


def test_owner_shifted_into_the_asset_column_rejects():
    lines = _lines("2021-20018021.pdf")
    i = next(i for i, l in enumerate(lines) if "Dominion" in l)
    a = lines[i].index("Dominion")
    lines[i] = " " * a + "SP " + lines[i][a:]
    with pytest.raises(ParseRejected, match="owner"):
        parse_layout_text("\n".join(lines))


def test_common_word_in_parentheses_is_not_upper_cased():
    from app.etf_check import ticker_candidates
    [t] = parse_pdf(OLD / "2015-20003115.pdf").trades
    assert (t.owner, t.asset_text) == ("SP", "root9b Techologies Inc. (RTNB) (stock)")
    assert ticker_candidates(t.asset_text) == ["RTNB"]


def test_district_of_columbia_bond_is_not_a_shifted_owner():
    [t] = parse_pdf(OLD / "2018-20010294.pdf").trades
    assert (t.owner, t.asset_text, t.asset_code) == ("self", "DC go boND 4.0% JUN01 2036", "GS")


def test_dc_owner_shifted_before_a_company_rejects():
    lines = _lines("2018-20010294.pdf")
    i = next(i for i, l in enumerate(lines) if "DC go boND" in l)
    lines[i] = lines[i].replace("DC go boND 4.0% JUN01 2036", "DC Apple Inc. (AAPL)     ")
    with pytest.raises(ParseRejected, match="owner"):
        parse_layout_text("\n".join(lines))
