import json
from datetime import date
from pathlib import Path

import pytest

from app.members import Term, load_terms, match_house, match_senate, norm_tokens, party_letter, service_periods

TERMS = load_terms(json.loads((Path(__file__).parent / "fixtures" / "reference" / "legislators_sample.json").read_text()))
ON = date(2026, 9, 9)


def test_norm_tokens():
    assert norm_tokens("Justice, II") == ["justice"]
    assert norm_tokens("McConnell, Jr.") == ["mcconnell"]
    assert norm_tokens("RICHARD ") == ["richard"]
    assert norm_tokens("Peña-Smith") == ["pena", "smith"]
    assert norm_tokens("O'Rourke") == norm_tokens("O’Rourke") == ["orourke"]   # House index ', member data ’


@pytest.mark.parametrize("first,last,sd,bioguide", [
    ("Brandon", "Gill", "TX26", "G000603"),
    ("April McClain", "Delaney", "MD06", "M001232"),   # index says Delaney, member data says McClain Delaney
    ("Scott", "Peters", "CA50", "P000608"),
    ("Neal Patrick", "Dunn, MD, FACS", "FL02", "D000628"),   # real index row: titles after the last name
])
def test_house_matches(first, last, sd, bioguide):
    r = match_house(TERMS, first, last, sd, ON)
    assert r.term and r.term.bioguide == bioguide


@pytest.mark.parametrize("first,last,sd,filed,bioguide", [
    ("Pete", "Sessions", "TX32", date(2021, 5, 3), "S000250"),           # real index row; he sat for TX17
    ("Richard Dean Dr", "McCormick", "GA06", date(2025, 9, 17), "M001218"),   # real; GA07 since 2025
    ("Pete", "Sessions", "TX26", ON, "S000250"),                         # that district's member is Gill
])
def test_house_matches_member_listed_under_another_district_of_the_state(first, last, sd, filed, bioguide):
    r = match_house(TERMS, first, last, sd, filed)
    assert r.term and r.term.bioguide == bioguide


@pytest.mark.parametrize("first,last,sd,filed", [
    ("John", "Sessions", "TX32", date(2021, 5, 3)),      # another first initial
    ("Pete", "Sessions", "TX32", date(2020, 6, 1)),      # between his terms, over a year after he left
    ("Steve", "Scott", "CA49", ON),                      # Scott is the first name of Scott H. Peters (CA50)
    ("Pete", "Jr.", "TX32", date(2021, 5, 3)),           # no last name left once the suffix is dropped
])
def test_house_member_of_another_district_needs_last_name_first_initial_and_term(first, last, sd, filed):
    assert match_house(TERMS, first, last, sd, filed).term is None


SMITHS = [Term(b, f"{f} Smith", f, "", "Smith", "rep", "TX", d, "Republican", date(2025, 1, 3), date(2027, 1, 3))
          for b, f, d in (("X000001", "Pat", 1), ("X000002", "Paul", 2))]


def test_house_member_of_another_district_must_be_the_only_one():
    r = match_house(SMITHS, "P.", "Smith", "TX03", ON)
    assert r.term is None and "ambiguous" in r.reason


def test_house_member_of_the_district_wins_over_namesakes_elsewhere():
    assert match_house(SMITHS, "P.", "Smith", "TX02", ON).term.bioguide == "X000002"


# Members who left on 2025-01-03; late reports by them are real (Manning: 108 days later, Perdue: 4 months).
FORMER = [Term("F000001", "Kathy Lane", "Kathy", "", "Lane", "rep", "NC", 6, "Democrat", date(2023, 1, 3), date(2025, 1, 3)),
          Term("F000002", "David Perdue", "David", "", "Perdue", "sen", "GA", None, "Republican", date(2019, 1, 3),
               date(2025, 1, 3))]


@pytest.mark.parametrize("first,filed,bioguide", [
    ("Kathy", date(2025, 4, 21), "F000001"),     # after the 90-day window, within a year: a late report
    ("Kathy", date(2026, 1, 4), None),           # over a year after she left
    ("Mary", date(2025, 4, 21), None),           # another first initial
])
def test_house_late_report_of_a_member_who_left(first, filed, bioguide):
    r = match_house(FORMER, first, "Lane", "NC06", filed)
    assert (r.term and r.term.bioguide) == bioguide


@pytest.mark.parametrize("first,filed,bioguide", [
    ("David A", date(2025, 5, 2), "F000002"),
    ("David A", date(2026, 1, 4), None),
    ("Mark", date(2025, 5, 2), None),
])
def test_senate_late_report_of_a_senator_who_left(first, filed, bioguide):
    r = match_senate(FORMER, first, "Perdue , Jr", filed)
    assert (r.term and r.term.bioguide) == bioguide


def test_house_name_mismatch_is_not_matched():
    r = match_house(TERMS, "John", "Smith", "TX26", ON)
    assert r.term is None and "name" in r.reason


def test_house_outside_term_is_not_matched():
    assert match_house(TERMS, "Brandon", "Gill", "TX26", date(2020, 1, 1)).term is None


@pytest.mark.parametrize("first,last,bioguide", [
    ("John", "Fetterman", "F000479"),
    ("James Conley", "Justice, II", "J000312"),
    ("A. Mitchell", "McConnell, Jr.", "M000355"),
    ("Angus S", "King, Jr.", "K000383"),
    ("Rick", "Scott", "S001217"),
    ("Tim", "Scott", "S001184"),
    ("SHELDON ", "WHITEHOUSE", "W000802"),
])
def test_senate_matches(first, last, bioguide):
    r = match_senate(TERMS, first, last, date(2026, 10, 7))
    assert r.term and r.term.bioguide == bioguide


def test_senate_ambiguous_without_matching_initial():
    r = match_senate(TERMS, "Xavier", "Scott", date(2026, 10, 7))
    assert r.term is None and "ambiguous" in r.reason


def test_party_letter():
    assert [party_letter(p) for p in ("Democrat", "Republican", "Independent")] == ["D", "R", "I"]


def test_service_periods_join_terms_across_swearing_in_but_keep_real_breaks():
    """Term ends and the next term's start can be days apart (Jan 3 to Jan 6, 2015); years out of office are a break."""
    term = {"state": "CA", "district": 1, "party": "Democrat"}
    people = [{"id": {"bioguide": "X1"}, "name": {"first": "Ann", "last": "Bee"}, "terms": [
        {**term, "type": "rep", "start": "2015-01-06", "end": "2017-01-03"},
        {**term, "type": "rep", "start": "2013-01-03", "end": "2015-01-03"},
        {**term, "type": "sen", "start": "2021-01-03", "end": "2027-01-03"}]}]
    assert service_periods(load_terms(people)) == {
        "X1": ((date(2013, 1, 3), date(2017, 1, 3)), (date(2021, 1, 3), date(2027, 1, 3)))}
