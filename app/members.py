"""Match report filers to members of Congress (spec C8) using the congress-legislators data."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

GRACE = timedelta(days=90)
LATE = timedelta(days=365)       # how long after leaving a member's late report is still matched to them
# not names: suffixes, and titles the House index puts in name fields ("Dunn, MD, FACS", "Richard Dean Dr")
_NOT_NAMES = {"jr", "sr", "ii", "iii", "iv", "dr", "md", "facs", "phd", "dvm"}


@dataclass(frozen=True)
class Term:
    bioguide: str
    full_name: str
    first: str
    nickname: str
    last: str
    chamber: str
    state: str
    district: int | None
    party: str
    start: date
    end: date


@dataclass(frozen=True)
class MatchResult:
    term: Term | None
    reason: str


def norm_tokens(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    s = s.replace("'", "")           # joined, as ’ is by the ascii step: House index O'Rourke, member data O’Rourke
    return [t for t in re.split(r"[^a-z]+", s) if t and t not in _NOT_NAMES]


def load_terms(people: list[dict]) -> list[Term]:
    out = []
    for p in people:
        n = p["name"]
        for t in p["terms"]:
            out.append(Term(p["id"]["bioguide"], n.get("official_full") or f'{n.get("first", "")} {n["last"]}',
                            n.get("first", ""), n.get("nickname", ""), n["last"], t["type"], t["state"],
                            t.get("district"), t.get("party", ""), date.fromisoformat(t["start"]),
                            date.fromisoformat(t["end"])))
    return out


SWEARING_IN = timedelta(days=14)   # a term ends Jan 3 and the next can start days later (2015-01-06): not a break


def service_periods(terms: list[Term]) -> dict[str, tuple[tuple[date, date], ...]]:
    """C11: each member's times in office (either chamber), oldest first, as (first day, last day)."""
    by: dict[str, list[tuple[date, date]]] = {}
    for t in sorted(terms, key=lambda t: t.start):
        out = by.setdefault(t.bioguide, [])
        if out and t.start - out[-1][1] <= SWEARING_IN:
            out[-1] = (out[-1][0], max(out[-1][1], t.end))
        else:
            out.append((t.start, t.end))
    return {k: tuple(v) for k, v in by.items()}


def _covers(t: Term, filed: date) -> bool:
    return t.start <= filed <= t.end + GRACE


def _unique(cands: list[Term], what: str) -> MatchResult:
    people = {t.bioguide: t for t in cands}
    if len(people) == 1:
        return MatchResult(next(iter(people.values())), "")
    return MatchResult(None, f"{what}: {'no candidate' if not people else 'ambiguous'}")


def _initial(name: str) -> str:
    return (norm_tokens(name) or [""])[0][:1]


def _same_initial(t: Term, first: str) -> bool:
    """The filer's first initial is that of the member's first name, nickname or official full name."""
    i = _initial(first)
    return bool(i) and any(_initial(x) == i for x in (t.first, t.nickname, t.full_name) if x)


def match_house(terms: list[Term], first: str, last: str, state_district: str, filed: date) -> MatchResult:
    m = re.fullmatch(r"([A-Z]{2})(\d{1,2}|AL)", state_district.strip().upper())
    if not m:
        return MatchResult(None, f"unrecognized state/district {state_district!r}")
    state, dist = m[1], 0 if m[2] == "AL" else int(m[2])
    reps = [t for t in terms if t.chamber == "rep" and t.state == state]
    want = norm_tokens(last)
    here = [t for t in reps if (t.district or 0) == dist and _covers(t, filed)
            and want and set(want) <= set(norm_tokens(t.last) + norm_tokens(t.full_name))]
    if here:
        return _unique(here, "house")
    # The index can give a district the member does not sit for (Sessions: TX32 in 2021, when he sat for TX17), and a
    # member who left can report late (Farr: five months after). Across the state and up to LATE after a term, the
    # name must be the member's last name and the first initial must agree.
    elsewhere = [t for t in reps if t.start <= filed <= t.end + LATE and want
                 and set(want) <= set(norm_tokens(t.last)) and _same_initial(t, first)]
    if elsewhere:
        return _unique(elsewhere, "house")
    if not any((t.district or 0) == dist and _covers(t, filed) for t in reps):
        return MatchResult(None, "no member for that district on the filing date")
    return MatchResult(None, "name does not match the member for that district")


def match_senate(terms: list[Term], first: str, last: str, filed: date) -> MatchResult:
    want = norm_tokens(last)
    sens = [t for t in terms if t.chamber == "sen" and norm_tokens(t.last) == want]
    cands = [t for t in sens if _covers(t, filed)]
    if not cands:                   # a senator who left can report late (Perdue: four months after)
        return _unique([t for t in sens if t.start <= filed <= t.end + LATE and _same_initial(t, first)], "senate")
    n = len({t.bioguide for t in cands})
    if n > 1:
        cands = [t for t in cands if _same_initial(t, first)]
        if len({t.bioguide for t in cands}) != 1:
            return MatchResult(None, f"senate: ambiguous, {n} senators named {last!r} and the first initial does not identify one")
    return _unique(cands, "senate")


def party_letter(party: str) -> str:
    return {"Democrat": "D", "Republican": "R", "Independent": "I"}.get(party, party[:1].upper())
