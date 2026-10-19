from array import array
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.congress_sim import parse_amount
from app.curated import ManagerFund
from app.leaderboards import (
    CTrade, DenseSeries, FilerHistory, MemberInfo, Position, PriceBook, assemble, congress_board,
    investors_board, last_quarter_end_on_or_before, latest_due_quarter, make_window, managers_board, required_periods,
    UNPRICED_MAX, quarter_ends_between, strip_detail,
)
from app.returns import simple_return, twr

TODAY = date(2026, 10, 8)


def business_days(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = business_days(date(2018, 1, 1), TODAY)
IDX = {d: i for i, d in enumerate(DAYS)}


def series(daily_growth):
    vals, v = array("d"), 1.0
    for _ in DAYS:
        vals.append(v)
        v *= 1 + daily_growth
    return DenseSeries(IDX, vals)


def closes(tri, scale=50.0):
    return DenseSeries(IDX, array("d", (tri[d] * scale for d in DAYS)))


TRI = {"SPY": series(0.0003), "AAA": series(0.0005), "BBB": series(0.0)}
BOOK = PriceBook(TRI, {k: closes(v) for k, v in TRI.items()}, DAYS, set())


def win(key):
    return make_window(key, BOOK, TODAY)


def spy(w):
    return simple_return(BOOK.tri["SPY"], w.start, w.end)


def test_dense_series_mapping():
    s = DenseSeries({date(2026, 1, 2): 0, date(2026, 1, 5): 1}, array("d", [1.0, float("nan")]))
    assert s[date(2026, 1, 2)] == 1.0 and date(2026, 1, 5) not in s and len(s) == 1
    with pytest.raises(KeyError):
        s[date(2026, 1, 5)]


def test_quarter_helpers():
    assert last_quarter_end_on_or_before(date(2025, 10, 8)) == date(2025, 9, 30)
    assert quarter_ends_between(date(2025, 9, 30), date(2026, 6, 30)) == [
        date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]
    assert latest_due_quarter(date(2026, 10, 8)) == date(2026, 6, 30)
    assert latest_due_quarter(date(2026, 8, 15)) == date(2026, 6, 30)
    assert latest_due_quarter(date(2026, 8, 14)) == date(2026, 3, 31)


def MF(person, ticker, since, start):
    return ManagerFund(person, ticker, f"{ticker} Fund", since, start, "https://x", "evidence", TODAY)


def test_managers_average_and_tenure():
    w = win("3y")
    funds = [MF("Ann", "AAA", "2014", date(2014, 12, 31)), MF("Ann", "BBB", "2014", date(2014, 12, 31)),
             MF("Bob", "AAA", "2025", date(2025, 12, 31))]
    ranked, unranked = managers_board(funds, BOOK, w, spy(w))
    [ann] = ranked
    expect = (simple_return(TRI["AAA"], w.start, w.end) + 0.0) / 2
    assert ann["total"] == pytest.approx(expect) and ann["badge"] == "EXACT"
    assert ann["annualized"] is not None and ann["vs_spx"] == pytest.approx((expect - spy(w)) * 100)
    [bob] = unranked
    assert bob["reason"] == "no fund managed for the whole window"
    assert "managed since 2025" in bob["detail"]["funds"][0]["reason"]


def test_managers_1y_has_no_annualized():
    w = win("1y")
    ranked, _ = managers_board([MF("Ann", "AAA", "2014", date(2014, 12, 31))], BOOK, w, spy(w))
    assert ranked[0]["annualized"] is None


def history(cik, name, ticker, shares, start_q, end_q, skip=(), misreport=1.0):
    periods, accs = {}, {}
    for q in quarter_ends_between(start_q, end_q):
        if q in skip:
            continue
        t = max(d for d in DAYS if d <= q)
        periods[q] = [Position("CUSIP" + ticker, ticker, shares, shares * BOOK.close[ticker][t] * misreport, ticker)]
        accs[q] = f"acc-{q}"
    return FilerHistory(cik, name, periods, accs)


def test_investor_ranked_matches_holding_return():
    w = win("3y")
    h = history("1", "Big Fund", "AAA", 1_000_000, date(2023, 6, 30), date(2026, 6, 30))
    ranked, unranked, issues = investors_board([h], {}, BOOK, w, spy(w), TODAY)
    [row] = ranked
    assert row["total"] == pytest.approx(simple_return(TRI["AAA"], w.start, w.end), rel=1e-9)
    assert row["badge"] == "APPROX" and row["n_etfs"] == 1 and row["top_etf"] == "AAA"
    assert row["n_periods"] == len(required_periods(h.periods, w, TODAY)) and row["priced_share"] == 1.0
    assert unranked == [] and issues == []


def test_investor_below_threshold_hidden_unless_famous():
    w = win("1y")
    small = history("2", "Small", "AAA", 10, date(2025, 9, 30), date(2026, 6, 30))
    assert investors_board([small], {}, BOOK, w, spy(w), TODAY)[:2] == ([], [])
    ranked, unranked, _ = investors_board([small], {"2": ("Famous Person", "founder")}, BOOK, w, spy(w), TODAY)
    assert ranked == [] and unranked[0]["reason"].startswith("ETF holdings below $10M")
    assert unranked[0]["person"] == "Famous Person"


def test_investor_missing_report():
    w = win("1y")
    h = history("3", "Gappy", "AAA", 1_000_000, date(2025, 9, 30), date(2026, 6, 30), skip={date(2026, 3, 31)})
    _, unranked, _ = investors_board([h], {}, BOOK, w, spy(w), TODAY)
    assert unranked[0]["reason"] == "missing report for 2026-03-31"


def test_investor_x2_mismatch_makes_position_unpriceable():
    w = win("1y")
    h = history("4", "Odd", "AAA", 1_000_000, date(2025, 9, 30), date(2026, 6, 30), misreport=2.0)
    _, unranked, issues = investors_board([h], {}, BOOK, w, spy(w), TODAY)
    assert unranked[0]["reason"] == "insufficient price data"
    assert issues and issues[0][0] == "x2-mismatch"


SPLIT_DAY = date(2026, 2, 2)          # a Monday between Q[1] and Q[2]


def test_split_factor_counts_only_splits_after_the_day():
    book = PriceBook(TRI, BOOK.close, DAYS, set(), {"SSS": [(SPLIT_DAY, 3.0), (date(2026, 5, 4), 0.5)]})
    assert book.split_factor_after("SSS", date(2026, 1, 30)) == 1.5
    assert book.split_factor_after("SSS", SPLIT_DAY) == 0.5        # that day's close is already post-split
    assert book.split_factor_after("SSS", date(2026, 5, 4)) == 1.0
    assert book.split_factor_after("AAA", date(2026, 1, 30)) == 1.0


def test_investor_position_split_after_quarter_end_is_priced():
    """Closes are split-adjusted (Yahoo), but a 13F reports the shares held at the quarter-end, before a later split."""
    book = PriceBook({**TRI, "SSS": TRI["AAA"]}, {**BOOK.close, "SSS": closes(TRI["AAA"])}, DAYS, set(),
                     {"SSS": [(SPLIT_DAY, 2.0)]})
    w = make_window("1y", book, TODAY)
    plan = {}
    for q in Q:
        t = max(d for d in DAYS if d <= q)
        shares = 1_000_000 if t < SPLIT_DAY else 2_000_000             # one old share became two new ones
        quoted = book.close["SSS"][t] * (2.0 if t < SPLIT_DAY else 1.0)  # the price as quoted on t
        plan[q] = [("SSS", shares, shares * quoted)]
    ranked, unranked, issues = investors_board([hist_manual("11", "Splitter", plan)], {}, book, w, spy(w), TODAY)
    assert issues == [] and unranked == []
    assert ranked[0]["total"] == pytest.approx(simple_return(book.tri["SSS"], w.start, w.end), rel=1e-12)


def test_reduced_days_give_same_total_as_daily():
    w = win("3y")
    h = history("1", "Big Fund", "AAA", 1_000_000, date(2023, 6, 30), date(2026, 6, 30))
    ranked, _, _ = investors_board([h], {}, BOOK, w, spy(w), TODAY)
    changes = [(max(d for d in DAYS if d <= q), "AAA", 1_000_000 * BOOK.close["AAA"][max(d for d in DAYS if d <= q)]
                / BOOK.tri["AAA"][max(d for d in DAYS if d <= q)]) for q in sorted(h.periods)]
    daily = twr(changes, BOOK.tri, DAYS, w.start, w.end)
    assert ranked[0]["total"] == pytest.approx(daily.total, rel=1e-12)


MEMBERS = {"M1": MemberInfo("M1", "Rep One", "House", "D", "CA"), "M2": MemberInfo("M2", "Rep Two", "House", "R", "TX"),
           "M3": MemberInfo("M3", "Sen Three", "Senate", "I", "ME"), "M4": MemberInfo("M4", "Rep Four", "House", "D", "NY")}


def CT(rid, ticker, kind, tx, amount="$1,001 - $15,000"):
    return CTrade(rid, 1, tx, tx, ticker, kind, parse_amount(amount), amount, "https://x/" + rid)


def test_congress_long_holder_ranked_with_range():
    w = win("5y")
    trades = {"M1": [CT("r1", "AAA", "buy", date(2019, 3, 4))]}
    ranked, unranked = congress_board(MEMBERS, trades, BOOK, w, spy(w))
    [row] = ranked
    assert row["total"] == pytest.approx(simple_return(TRI["AAA"], w.start, w.end))
    assert row["low"] == pytest.approx(row["total"]) and row["high"] == pytest.approx(row["total"])
    assert (row["badge"], row["party_state"], row["chamber"]) == ("EST", "D-CA", "House")


def test_congress_recent_buyer_not_enough_days():
    w = win("1y")
    trades = {"M2": [CT("r2", "AAA", "buy", date(2026, 8, 13), "$50,001 - $100,000")]}
    ranked, unranked = congress_board(MEMBERS, trades, BOOK, w, spy(w))
    assert ranked == [] and unranked[0]["reason"].startswith("invested on ")
    assert unranked[0]["total"] is not None


def test_congress_unpriceable_majority():
    w = win("1y")
    trades = {"M3": [CT("r3", "ZZZ", "buy", date(2024, 1, 3), "$15,001 - $50,000"),
                     CT("r4", "AAA", "buy", date(2024, 1, 3))]}
    ranked, unranked = congress_board(MEMBERS, trades, BOOK, w, spy(w))
    assert unranked[0]["reason"] == "insufficient price data"


def test_congress_closed_before_window_not_listed():
    w = win("1y")
    trades = {"M4": [CT("r5", "AAA", "buy", date(2019, 3, 4)), CT("r6", "AAA", "sell_full", date(2020, 3, 4))]}
    assert congress_board(MEMBERS, trades, BOOK, w, spy(w)) == ([], [])


def served(*periods):
    return {"M1": replace(MEMBERS["M1"], service=tuple((date(*a), date(*b)) for a, b in periods))}


def test_congress_member_who_left_during_the_window_is_not_ranked():
    """C11: trades are reported only while in office, so holdings after a member leaves are unknown."""
    w = win("5y")
    trades = {"M1": [CT("r1", "AAA", "buy", date(2019, 3, 4))]}
    ranked, unranked = congress_board(served(((2015, 1, 6), (2025, 1, 3))), trades, BOOK, w, spy(w))
    assert ranked == [] and [r["reason"] for r in unranked] == [
        "left office on 2025-01-03; trades after that are not reported"]


def test_congress_member_who_left_before_the_window_is_not_listed():
    w = win("1y")
    trades = {"M1": [CT("r1", "AAA", "buy", date(2019, 3, 4))]}       # never reported sold
    assert congress_board(served(((2015, 1, 6), (2023, 1, 3))), trades, BOOK, w, spy(w)) == ([], [])


def test_congress_break_in_service_inside_the_window_is_not_ranked():
    w = win("5y")
    trades = {"M1": [CT("r1", "AAA", "buy", date(2019, 3, 4))]}
    ranked, unranked = congress_board(served(((2015, 1, 6), (2023, 1, 3)), ((2024, 2, 13), (2027, 1, 3))),
                                      trades, BOOK, w, spy(w))
    assert ranked == [] and [r["reason"] for r in unranked] == [
        "out of office from 2023-01-03 to 2024-02-13; trades in that time are not reported"]


def test_congress_trades_from_an_earlier_time_in_office_are_not_counted():
    """Holdings end when a member leaves; after a break (Lummis: House to 2017, Senate from 2021) the record restarts."""
    w = win("1y")
    trades = {"M1": [CT("r1", "AAA", "buy", date(2018, 3, 5)), CT("r2", "BBB", "buy", date(2021, 3, 1))]}
    [row], _ = congress_board(served(((2015, 1, 6), (2019, 1, 3)), ((2021, 1, 3), (2027, 1, 3))), trades, BOOK, w, spy(w))
    assert row["total"] == pytest.approx(simple_return(TRI["BBB"], w.start, w.end)) and row["n_trades"] == 1
    assert row["priced_share"] == 1.0
    assert {"ref": "until 2019-01-03", "message": "1 trade from an earlier time in office not counted: holdings "
                                                   "end when a member leaves office"} in row["detail"]["notes"]


def test_assemble_and_strip():
    w = win("1y")
    board = assemble("managers", w, [{"id": "b", "name": "B", "total": 0.1, "detail": {"x": 1}},
                                     {"id": "a", "name": "A", "total": 0.2, "detail": {}}], [], 0.05, "now")
    assert [r["rank"] for r in board["ranked"]] == [1, 2] and board["ranked"][0]["name"] == "A"
    assert board["benchmark"] == {"ticker": "SPY", "total": 0.05}
    assert "detail" not in strip_detail(board)["ranked"][0]


# ---------- review fixes ----------

GAP_END = date(2026, 3, 2)


def gapped_book():
    vals, v = array("d"), 1.0
    for d in DAYS:
        vals.append(v if d <= GAP_END else float("nan"))
        v *= 1.0002
    ccc = DenseSeries(IDX, vals)
    tri = {**TRI, "CCC": ccc}
    return PriceBook(tri, {k: (closes(v) if k != "CCC" else DenseSeries(IDX, array("d", (x * 50 for x in vals))))
                           for k, v in tri.items()}, DAYS, set())


def hist_manual(cik, name, plan):
    """plan: {quarter_end: [(ticker, shares, reported_usd)]}"""
    return FilerHistory(cik, name, {q: [Position("C" + t, t, s, r, t) for t, s, r in pos] for q, pos in plan.items()},
                        {q: f"acc-{q}" for q in plan})


Q = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]


def test_investor_gapped_etf_unranked_not_crash():
    book = gapped_book()
    w = make_window("1y", book, TODAY)
    spy_t = simple_return(book.tri["SPY"], w.start, w.end)
    plan = {Q[0]: [("CCC", 1_000_000, 5e7)], Q[1]: [("CCC", 1_000_000, 5e7)],
            Q[2]: [("AAA", 1_000_000, 5e7)], Q[3]: [("AAA", 1_000_000, 5e7)]}
    ranked, unranked, _ = investors_board([hist_manual("9", "Gap", plan)], {}, book, w, spy_t, TODAY)
    assert ranked == [] and unranked[0]["reason"].startswith("insufficient price data")


def test_investor_small_gapped_position_dropped_stays_ranked():
    book = gapped_book()
    w = make_window("1y", book, TODAY)
    spy_t = simple_return(book.tri["SPY"], w.start, w.end)
    big = lambda: ("AAA", 1_000_000, 1_000_000 * 50 * 1.0)  # noqa: E731  (X2 checked below via close lookups)
    plan = {}
    for q in Q:
        t = max(d for d in DAYS if d <= q)
        pos = [("AAA", 1_000_000, 1_000_000 * book.close["AAA"][t])]
        if q == Q[1]:
            pos.append(("CCC", 1000, 1000 * book.close["CCC"][t]))
        plan[q] = pos
    ranked, unranked, _ = investors_board([hist_manual("8", "Mostly", plan)], {}, book, w, spy_t, TODAY)
    assert len(ranked) == 1 and unranked == []
    assert 1 - UNPRICED_MAX <= ranked[0]["priced_share"] < 1        # the worst quarter, with CCC unpriced


def test_congress_gapped_etf_unranked_not_crash():
    book = gapped_book()
    w = make_window("1y", book, TODAY)
    trades = {"M1": [CT("g1", "CCC", "buy", date(2024, 1, 3))]}
    ranked, unranked = congress_board(MEMBERS, trades, book, w, 0.0)
    assert ranked == [] and unranked[0]["reason"].startswith("insufficient price data")


def test_investor_series_not_resubsampled():
    w = win("3y")
    h = history("1", "Big Fund", "AAA", 1_000_000, date(2023, 6, 30), date(2026, 6, 30))
    ranked, _, _ = investors_board([h], {}, BOOK, w, spy(w), TODAY)
    assert len(ranked[0]["detail"]["series"]) >= 150


def test_x2_issues_only_for_listed_or_famous():
    w = win("1y")
    h = history("5", "Tiny Odd", "AAA", 10, date(2025, 9, 30), date(2026, 6, 30), misreport=2.0)
    assert investors_board([h], {}, BOOK, w, spy(w), TODAY)[2] == []
    issues = investors_board([h], {"5": ("P", "r")}, BOOK, w, spy(w), TODAY)[2]
    assert issues


def test_x2_whole_filing_unit_error():
    w = win("1y")
    def plan(bad_both):
        out = {}
        for q in Q:
            t = max(d for d in DAYS if d <= q)
            a = 1_000_000 * BOOK.close["AAA"][t]
            b = 1_000_000 * BOOK.close["BBB"][t]
            out[q] = [("AAA", 1_000_000, a * 2), ("BBB", 1_000_000, b * (2 if bad_both else 1))]
        return out
    both = investors_board([hist_manual("6", "Units", plan(True))], {}, BOOK, w, spy(w), TODAY)[2]
    one = investors_board([hist_manual("7", "OneBad", plan(False))], {}, BOOK, w, spy(w), TODAY)[2]
    assert sum(1 for i in both if i[0] == "x2-likely-unit-error") == len(Q)
    assert not any(i[0] == "x2-likely-unit-error" for i in one)


def test_congress_same_day_buy_and_full_sale_before_window_not_counted():
    w = win("1y")
    trades = {"M4": [CT("s1", "AAA", "buy", date(2020, 3, 4)), CT("s2", "AAA", "sell_full", date(2020, 3, 4))]}
    assert congress_board(MEMBERS, trades, BOOK, w, spy(w)) == ([], [])


def test_investor_rebalance_matches_daily_twr():
    w = win("1y")
    plan = {Q[0]: [("AAA", 1_000_000)], Q[1]: [("AAA", 1_000_000), ("BBB", 2_000_000)],
            Q[2]: [("BBB", 2_000_000)], Q[3]: [("BBB", 2_000_000)]}
    full, changes, held = {}, [], set()
    for q, pos in plan.items():
        t = max(d for d in DAYS if d <= q)
        full[q] = [Position("C" + k, k, s, s * BOOK.close[k][t], k) for k, s in pos]
        units = {k: s * BOOK.close[k][t] / BOOK.tri[k][t] for k, s in pos}
        changes += [(t, k, 0.0) for k in held - set(units)] + [(t, k, u) for k, u in units.items()]
        held = set(units)
    h = FilerHistory("10", "Rebal", full, {q: f"acc-{q}" for q in plan})
    ranked, _, _ = investors_board([h], {}, BOOK, w, spy(w), TODAY)
    assert ranked[0]["total"] == pytest.approx(twr(changes, BOOK.tri, DAYS, w.start, w.end).total, rel=1e-12)


def test_congress_small_gapped_position_dropped():
    book = gapped_book()
    w = make_window("1y", book, TODAY)
    trades = {"M1": [CT("a1", "AAA", "buy", date(2024, 1, 3), "$250,001 - $500,000"),
                     CT("c1", "CCC", "buy", date(2025, 11, 3))]}
    ranked, unranked = congress_board(MEMBERS, trades, book, w, 0.0)
    [row] = ranked
    assert row["total"] == pytest.approx(simple_return(book.tri["AAA"], w.start, w.end))
    flags = {t["ticker"]: t["priced"] for t in row["detail"]["trades"]}
    assert flags == {"AAA": True, "CCC": False}


def test_congress_large_gapped_position_unranked():
    book = gapped_book()
    w = make_window("1y", book, TODAY)
    trades = {"M1": [CT("a1", "AAA", "buy", date(2024, 1, 3)),
                     CT("c1", "CCC", "buy", date(2025, 11, 3), "$50,001 - $100,000")]}
    ranked, unranked = congress_board(MEMBERS, trades, book, w, 0.0)
    assert ranked == [] and unranked[0]["reason"] == "insufficient price data"
