from datetime import date
from pathlib import Path

import pytest

from app.sources.sec13f import (
    DataSet, Filing, Holding, accession_from_path, aggregate, dataset_range, effective_holdings,
    filing_totals, iter_share_rows, list_dataset_urls, parse_current_atom, parse_form_idx,
    parse_infotable_xml, parse_primary_doc, read_filings, value_usd,
)

FIX = Path(__file__).parent / "fixtures" / "sec13f"
ACC = "0001172661-26-003822"


@pytest.fixture
def ds():
    return DataSet(FIX / "diameter_2026q2_restatement")


def test_read_filings(ds):
    f = read_filings(ds)[ACC]
    assert f == Filing(ACC, "1727012", "Diameter Capital Partners LP", date(2026, 6, 30), date(2026, 8, 14),
                       "13F-HR/A", "RESTATEMENT")


def test_share_rows_exclude_options_and_bonds(ds):
    rows = list(iter_share_rows(ds, read_filings(ds)))
    assert len(rows) == 28                         # 34 rows - 1 call - 1 put - 4 principal-amount rows
    cusips = {r.cusip for r in rows}
    assert "78462F103" not in cusips               # SPY puts excluded
    assert "09173BAA5" not in cusips               # convertible bond excluded
    echostar = [r for r in rows if r.cusip == "278768106"]
    assert [(r.shares, r.value_usd) for r in echostar] == [(326754.0, 33165531.0)]  # call row dropped
    iau = next(r for r in rows if r.cusip == "464285204")
    assert (iau.shares, iau.value_usd, iau.issuer) == (3363600.0, 253985436.0, "ISHARES GOLD TR")


def test_value_units_switch_on_2023_01_03():
    assert value_usd("1234", date(2023, 1, 2)) == 1234000.0
    assert value_usd("1234", date(2023, 1, 3)) == 1234.0


def test_aggregate_sums_same_cusip():
    hs = [Holding("a", "X", 10, 100, "i", "t"), Holding("a", "X", 5, 50, "i", "t"), Holding("a", "Y", 1, 1, "i", "t")]
    agg = aggregate(hs)
    assert agg["a"]["X"].shares == 15 and agg["a"]["X"].value_usd == 150 and agg["a"]["Y"].shares == 1


def test_filing_totals(ds):
    assert filing_totals(ds, read_filings(ds))[ACC] == (34, 1240161751.0)


def F(acc, form, amend, filed):
    return Filing(acc, "1", "Fund", date(2026, 3, 31), filed, form, amend)


def test_restatement_replaces_original():
    fs = [F("a1", "13F-HR", "", date(2026, 5, 1)), F("a2", "13F-HR/A", "RESTATEMENT", date(2026, 5, 2))]
    by = {"a1": {"X": Holding("a1", "X", 10, 1, "", "")}, "a2": {"Y": Holding("a2", "Y", 3, 1, "", "")}}
    eff, unknown = effective_holdings(fs, by)
    assert set(eff[("1", date(2026, 3, 31))]) == {"Y"} and unknown == []


def test_new_holdings_adds_rows():
    fs = [F("a2", "13F-HR/A", "NEW HOLDINGS", date(2026, 5, 2)), F("a1", "13F-HR", "", date(2026, 5, 1))]
    by = {"a1": {"X": Holding("a1", "X", 10, 1, "", "")},
          "a2": {"X": Holding("a2", "X", 5, 1, "", ""), "Z": Holding("a2", "Z", 1, 1, "", "")}}
    eff, _ = effective_holdings(fs, by)
    h = eff[("1", date(2026, 3, 31))]
    assert h["X"].shares == 15 and h["Z"].shares == 1


def test_restatement_with_no_etf_rows_empties_holdings():
    fs = [F("a1", "13F-HR", "", date(2026, 5, 1)), F("a2", "13F-HR/A", "RESTATEMENT", date(2026, 5, 2))]
    eff, _ = effective_holdings(fs, {"a1": {"X": Holding("a1", "X", 10, 1, "", "")}})
    assert eff[("1", date(2026, 3, 31))] == {}


def test_unknown_amendment_type_is_reported():
    fs = [F("a1", "13F-HR", "", date(2026, 5, 1)), F("a2", "13F-HR/A", "", date(2026, 5, 2))]
    eff, unknown = effective_holdings(fs, {"a1": {"X": Holding("a1", "X", 10, 1, "", "")}})
    assert unknown == ["a2"] and set(eff[("1", date(2026, 3, 31))]) == {"X"}


def test_xml_matches_dataset_rows(ds):
    filing = read_filings(ds)[ACC]
    from_xml = sorted(parse_infotable_xml((FIX / "diameter_infotable.xml").read_bytes(), filing),
                      key=lambda h: (h.cusip, h.shares))
    from_tsv = sorted(iter_share_rows(ds, read_filings(ds)), key=lambda h: (h.cusip, h.shares))
    assert [(h.cusip, h.shares, h.value_usd) for h in from_xml] == [(h.cusip, h.shares, h.value_usd) for h in from_tsv]


def test_primary_doc():
    f = parse_primary_doc((FIX / "diameter_primary_doc.xml").read_bytes(), ACC, "1727012", date(2026, 8, 14), "13F-HR/A")
    assert (f.period, f.filer_name, f.amendment_type) == (date(2026, 6, 30), "Diameter Capital Partners LP", "RESTATEMENT")


def test_form_idx():
    entries = parse_form_idx((FIX / "form.20260814.trimmed.idx").read_text())
    assert len(entries) == 8
    assert entries[1].form == "1-A POS"
    dia = [e for e in entries if e.cik == "1727012"]
    assert [(e.form, accession_from_path(e.path)) for e in dia] == [
        ("13F-HR", "0001172661-26-003510"), ("13F-HR/A", "0001172661-26-003822")]
    assert dia[0].filed == date(2026, 8, 14)


def test_current_atom():
    items = parse_current_atom((FIX / "current_13F-HR.atom").read_bytes())
    assert len(items) == 40
    assert items[0] == ("13F-HR", "1902501", "0001902501-26-000006", date(2026, 10, 8))


def test_dataset_links():
    urls = list_dataset_urls((FIX / "datasets_page.html").read_text())
    assert len(urls) == 54
    assert urls[0] == ("https://www.sec.gov/files/datastandardsinnovation/data/form-13f-data-sets/"
                       "01jun2026-31aug2026_form13f.zip")
    assert dataset_range(urls[0]) == (date(2026, 6, 1), date(2026, 8, 31))


def test_same_day_original_applies_before_restatement():
    # accession order must not matter: the original "z9" sorts after the restatement "a1" on the same day
    fs = [F("a1", "13F-HR/A", "RESTATEMENT", date(2026, 5, 1)), F("z9", "13F-HR", "", date(2026, 5, 1))]
    by = {"z9": {"X": Holding("z9", "X", 10, 1, "", "")}, "a1": {"Y": Holding("a1", "Y", 3, 1, "", "")}}
    eff, unknown = effective_holdings(fs, by)
    assert eff[("1", date(2026, 3, 31))] == by["a1"] and unknown == []


def test_new_holdings_13f_nt_excluded_by_read_filings(tmp_path):
    (tmp_path / "SUBMISSION.tsv").write_text(
        "ACCESSION_NUMBER\tCIK\tSUBMISSIONTYPE\tPERIODOFREPORT\tFILING_DATE\n"
        "0000000001-26-000001\t1\t13F-NT\t30-JUN-2026\t01-AUG-2026\n"
        "0000000001-26-000002\t1\t13F-HR\t30-JUN-2026\t01-AUG-2026\n")
    (tmp_path / "COVERPAGE.tsv").write_text(
        "ACCESSION_NUMBER\tAMENDMENTTYPE\tFILINGMANAGER_NAME\n"
        "0000000001-26-000001\t\tFund\n0000000001-26-000002\t\tFund\n")
    filings = read_filings(DataSet(tmp_path))
    assert set(filings) == {"0000000001-26-000002"}


def test_second_original_later_replaces_earlier_amendment():
    fs = [F("o1", "13F-HR", "", date(2026, 5, 1)), F("a1", "13F-HR/A", "NEW HOLDINGS", date(2026, 5, 2)),
          F("o2", "13F-HR", "", date(2026, 5, 3))]
    by = {"o1": {"X": Holding("o1", "X", 10, 1, "", "")},
          "a1": {"Z": Holding("a1", "Z", 1, 1, "", "")},
          "o2": {"Y": Holding("o2", "Y", 3, 1, "", "")}}
    eff, unknown = effective_holdings(fs, by)
    assert eff[("1", date(2026, 3, 31))] == by["o2"] and unknown == []


def test_dataset_range_old_quarterly_names():
    base = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    assert dataset_range(base + "2013q2_form13f.zip") == (date(2013, 4, 1), date(2013, 6, 30))
    assert dataset_range(base + "2014q1_form13f.zip") == (date(2014, 1, 1), date(2014, 3, 31))
    assert dataset_range(base + "2023q4_form13f.zip") == (date(2023, 10, 1), date(2023, 12, 31))
    with pytest.raises(ValueError):
        dataset_range(base + "2023q5_form13f.zip")
