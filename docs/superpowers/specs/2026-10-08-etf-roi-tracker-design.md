# ETF Return Leaders: Design Spec

- **Date:** 2026-10-08
- **Status:** Draft for review
- **Methodology version:** v1

## 1. Purpose

A web page that runs on the user's computer and ranks three groups of people by the return they have earned on ETFs over the last 1, 3 and 5 years, updating itself while it runs:

1. **Fund managers**: the people who run actively managed ETFs.
2. **Big investors**: institutional managers that file SEC Form 13F, with a filter for about 40 well-known investors.
3. **Congress**: members of the House and Senate, from their STOCK Act trade reports.

## 2. What "no mistakes" means here

The precision of the inputs differs by group, so the tool's correctness promise is defined as follows:

- Every number follows the rules in this document, applied to inputs that can be traced to an original public document.
- Every number is labeled with how precise its inputs are: **Exact** (fund managers), **Approximate** (13F) or **Estimate** (Congress).
- Nothing is dropped silently. Anything skipped, unmatched or failing a check is listed with its reason (§11).
- Rules are tested against hand-worked cases and real saved documents (§16).

The tool cannot make imprecise disclosures precise. Congress trade sizes are ranges, and 13F filings show only quarter-end snapshots. The page states this wherever those numbers appear.

## 3. Scope

**In scope:** the three groups above; time windows 1Y, 3Y and 5Y; live updates while the app runs; a local web page; source links for every input; a data-issues list; a methodology section.

**Out of scope:** online hosting or access from other devices; custom date ranges; alerts or notifications; investment advice; annual financial-disclosure (holdings) reports of Congress; reading scanned or handwritten reports (OCR); all ETF managers (only a curated list); history of past leaderboards; auto-start at login.

## 4. Data sources

All sources were checked as reachable on 2026-10-08.

| Source | Used for | Access | Limits / terms |
|---|---|---|---|
| Yahoo Finance chart API (`query1.finance.yahoo.com/v8/finance/chart`, `/v8/finance/spark`) | Daily closes, dividends, splits, instrument type; live prices (spark: 20 symbols per request) | Unofficial, no key. Requests must use a custom User-Agent, because browser-like agents get HTTP 429 | Back off on 429; personal use |
| SEC Form 13F data sets (quarterly ZIPs, about 100 MB each, from `sec.gov/data-research/sec-markets-data/form-13f-data-sets`) | 13F holdings history | Free | SEC fair access allows at most 10 requests/s with a User-Agent that includes a contact email. The app uses at most 5 requests/s |
| SEC EDGAR current-filings feed and daily index | New 13F filings between data-set releases | Free | Same as above |
| OpenFIGI mapping API (`api.openfigi.com/v3/mapping`) | CUSIP → ticker and security type | Free. An optional free key raises the rate limit | Respect 429 and the published limits |
| Nasdaq Trader symbol directory (`nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt`) | Names and ETF flags of currently listed US securities, used to match Congress assets reported without a ticker | Free | Daily file |
| House Clerk financial disclosures (`disclosures-clerk.house.gov`): yearly index `{YEAR}FD.zip`, reports `ptr-pdfs/{YEAR}/{DocID}.pdf` | House trade reports (index rows with FilingType `P`) | Free, no browser needed | At most 2 requests/s. Ethics in Government Act use restrictions (below) |
| Senate eFD (`efdsearch.senate.gov`) | Senate trade reports | Rejects plain HTTP clients (403); works in a real browser after ticking the terms checkbox. Automated with Playwright (Chromium) | Ethics in Government Act use restrictions (below) |
| congress-legislators (`unitedstates.github.io/congress-legislators/legislators-{current,historical}.json`) | Party, state, district and term dates for matching filers to members | Free | — |

**Use restrictions.** House and Senate disclosure reports may not be obtained or used for any unlawful purpose, for any commercial purpose (other than news media), for credit ratings or for soliciting money. The tool is for personal, non-commercial use. The page footer says so, and first-run setup asks the user to accept the Senate terms before the Senate source is enabled (§15).

## 5. Architecture

A single Python process runs a local web server, a background scheduler and an SQLite database. The page is plain HTML/CSS/JS with no build step.

```
STOCKS TEST/
├── run.sh                 first-run setup, then starts the app and opens the browser
├── config.toml            user settings (created by run.sh; not shared)
├── requirements.txt       pinned: pypdf, playwright, pytest
├── app/
│   ├── server.py          HTTP server bound to 127.0.0.1:8765; page + JSON API (§14)
│   ├── scheduler.py       background jobs (§10)
│   ├── db.py              SQLite schema and access (data/tracker.db)
│   ├── returns.py         pure return math; no I/O
│   ├── prices.py          price provider interface + Yahoo implementation + local cache
│   ├── etf_check.py       ETF identification and ticker resolution (§9)
│   ├── members.py         filer → member of Congress matching (§8.3)
│   ├── leaderboards.py    applies group rules, builds rankings per group and window
│   ├── issues.py          the data-issues log (§11)
│   ├── curated/
│   │   ├── managers.json          fund managers, each entry with sources (§8.1)
│   │   └── famous_investors.json  well-known 13F filers, each entry with sources (§8.2)
│   └── sources/
│       ├── managers.py    loads and validates managers.json
│       ├── sec13f.py      13F data sets + new filings: fetch, parse
│       ├── house.py       House index + PDF reports: fetch, parse
│       └── senate.py      Senate eFD via Playwright: fetch, parse
├── web/                   index.html, app.js, styles.css
├── data/                  tracker.db + raw downloaded originals (cache)
├── docs/superpowers/specs/
└── tests/                 unit tests; fixtures/ holds real saved documents with a manifest of their URLs
```

**Data flow:** fetch originals (saved to `data/raw/`) → parse into normalized records → resolve tickers and confirm ETFs (§9) → fetch prices → compute per-person returns (§7, §8) → write leaderboards → the page polls for changes.

**Module contracts:**
- Each source module has `fetch(...)`, which uses the network and saves the raw file and returns its path, and `parse(path)`, which is pure and returns normalized records or raises `ParseRejected(reason)`. Parsers are tested only on saved real documents.
- `returns.py` takes plain data (price series, unit changes) and returns numbers. It has no network, database or clock access; "today" is passed in.
- `prices.py` exposes `history(ticker) -> [(date, close, dividend, split)]` and `latest(tickers) -> {ticker: (price, as_of)}`. Only the Yahoo implementation knows Yahoo's URLs and formats.
- `leaderboards.py` reads normalized records and prices from the database and calls `returns.py`. It writes one JSON result per group and window.

## 6. Storage (SQLite, `data/tracker.db`)

| Table | Contents |
|---|---|
| `prices_daily` | ticker, date, close (split-adjusted), dividend (split-adjusted, by ex-date), split ratio. Completed sessions only |
| `prices_live` | ticker, price, as_of |
| `securities` | ticker, name, Yahoo type, OpenFIGI type, is_etf, evidence (raw responses), checked_at |
| `cusip_map` | cusip, ticker, OpenFIGI type, exchange code, raw response, checked_at |
| `f13_filings` | accession, CIK, filer name, period of report, filing date, form type, amendment type |
| `f13_holdings` | accession, CUSIP, shares, reported value in USD (unit-normalized), issuer name, title of class. Only rows whose CUSIP is identified as an ETF, or is unidentified but ETF-named (§8.2 F6) |
| `f13_filing_totals` | accession, number of rows, total reported value (all holdings), for sanity checks. For filings from SEC data sets this is the summary page's total, which also counts options and principal amounts and is sometimes entered in other units than the information table |
| `f13_books` | accession, book: the reported value of every share row of the filing (§8.2 F3 rows, ETF or not), in the same units as its ETF rows (F11) |
| `f13_period_stats` | CIK, quarter-end, reported ETF value, book (F2 applied). Written only by data rebuilds; live rebuilds read it (§10) |
| `congress_reports` | report id, chamber, filer name, member key, filing date, URL, status (`parsed`, `scanned`, `rejected`, `superseded`), reason |
| `congress_trades` | report id, row number, owner (self, SP, JT, DC), asset text, asset type code/label, ticker, transaction type, transaction date, amount low, amount high |
| `members` | member key, full name, chamber, party, state, district, bioguide id |
| `issues` | id, source, kind, reference, message, original URL, first seen, last seen, resolved |
| `source_status` | source, last success, last error, counters |
| `leaderboards` | group, window, JSON, computed_at, data version |
| `board_details` | group, window, person id, the whole row with its details panel (JSON), data version. A 13F row lists its quarter-ends by hash |
| `board_periods` | hash, one 13F quarter-end of a details panel (JSON). Each distinct one is stored once; a rebuild writes only new ones and removes those no row lists |

Raw originals (13F ZIPs, House PDFs, Senate HTML pages, index files) are kept under `data/raw/` so any parse can be re-run without downloading again.

## 7. Return methodology (all groups)

**R1. Metric.** The time-weighted return of the person's ETF holdings, dividends reinvested.

**R2. Total-return index (TRI) per ETF.** Using Yahoo's split-adjusted daily close `c(d)` and split-adjusted cash dividend `D(d)` with ex-date `d`:

```
TRI(first day) = 1
TRI(d) = TRI(prev) × (c(d) + D(d)) / c(prev)      prev = previous trading day of that ETF
```

**R3. Live value.** While a live price `p` is newer than the last completed session `L`:

```
TRI(now) = TRI(L) × (p + D(today)) / c(L)
```

Here `D(today)` is a dividend with ex-date today, if already known (otherwise 0). Daily history never includes the in-progress session, so the live price is never double-counted.

**R4. Window.** For N ∈ {1, 3, 5}:
- The end point `E` is the latest available value (live price if newer, else the last close).
- The start `S` is the last trading day on or before the date N calendar years before today. If that date is Feb 29 it becomes Feb 28. The start value is the close on `S`.
- Trading days are the dates in SPY's daily history, plus today while a live session is running.

**R5. Single-ETF window return.** `R = TRI(E) / TRI(S) − 1`.

**R6. Portfolio engine** (used by 13F and Congress).
- Holdings are units of TRI: buying `$A` of ETF i on day d adds `A / TRI_i(d)` units. Units change only at the close of a day.
- Portfolio value: `V(t) = Σ_i u_i(t) × TRI_i(t)`.
- Daily return using the units held before day t's changes:

```
r(t) = Σ_i u_i(t−1) × TRI_i(t) / V(t−1) − 1      only for days with V(t−1) > 0
TWR  = Π_{t ∈ (S, E]} (1 + r(t)) − 1
```

**R7. Invested fraction.** The number of trading days t in `(S, E]` with `V(t−1) > 0`, divided by the number of trading days in `(S, E]`.

**R8. Annualized** (3Y and 5Y only): `A = (1 + R)^(365.25 / days) − 1`, where `days` is the calendar days from `S` to the date of `E`.

**R9. Benchmark.** SPY's R5 return over the same window. "vs S&P" = person's total return − SPY total return, in percentage points.

**R10. Ranking.** Ranked people are sorted by total return over the window, highest first, with ties broken alphabetically by name. Display uses one decimal place.

**R11. Price coverage.**
- Positions are unpriceable when Yahoo has no data for the period or the ETF failed check X1 (§12).
- The unpriceable share is measured per group:
  - **13F:** at each required quarter-end (F7), the reported value of unpriceable ETF positions divided by the reported value of all ETF positions.
  - **Congress:** the midpoint amounts of purchases in unpriceable ETFs divided by the midpoint amounts of all ETF purchases. Both sums count only purchases dated on or before `E` whose position was still open at `S`, or opened after it. A position counts as closed only after a Sale (Full); partial sales of unpriceable positions leave it open, which is the conservative choice.
- If the unpriceable share is more than 5%, the person goes to **Not enough data** with the reason "insufficient price data".
- Otherwise those positions are removed, weights are renormalized, and the removal is shown in the person's details panel.

**R12. Not enough data.** People who fail a group eligibility rule are listed in a collapsed **Not enough data** section with the specific reason and their computed figures where computable. They are never ranked. §8.2 defines the one exception: the 13F listing threshold.

## 8. Group rules

### 8.1 Fund managers (badge: Exact)

- **M1. Who is listed.** About 30–40 people who are named portfolio managers of actively managed, US-listed ETFs.
  - Selection draws on the largest actively managed equity ETFs plus a few widely followed ones. The selection is a documented judgment call and the Methodology section says so.
  - The list holds current managers only.
- **M2. Each entry in `managers.json`** records:
  - person, and for each fund: ticker, fund name, manager-since date;
  - source URL (the fund's current prospectus or the issuer's fund page);
  - the quoted sentence that states the tenure;
  - the date it was verified.
- **M3. Tenure dates.**
  - If the source gives only a year, tenure starts on Dec 31 of that year.
  - If it gives a month and year, tenure starts on the last day of that month.
  - This is conservative: it never credits a manager with returns from before they joined.
- **M4. Qualifying funds.** A fund counts for a window only if the manager's tenure started on or before `S` and the fund has prices covering `[S, E]`.
- **M5. Return.** The person's return is the plain average of the R5 returns of their qualifying funds (equal weight, buy-and-hold from `S`). Annualized per R8.
- **M6. Eligibility.** At least one qualifying fund; otherwise Not enough data with "no fund managed for the whole window". The details panel lists every fund with its tenure, and the reason any fund doesn't count.
- **M7. Market price.** Returns are market-price total returns. These can differ slightly from the official NAV returns the issuer publishes, and the Methodology section says so.

### 8.2 Big investors, 13F (badge: Approximate)

- **F1. Inputs.** 13F-HR filings from the quarterly 13F data sets (history) and from EDGAR's current-filings feed and daily index (new filings). 13F-NT notices, which contain no holdings, are ignored.
- **F2. Amendments.** For each (CIK, period of report), start with the original 13F-HR. Apply 13F-HR/A filings in filing order:
  - an amendment of type RESTATEMENT replaces the holdings set;
  - an amendment of type NEW HOLDINGS adds rows.
- **F3. Rows used.**
  - Only share positions (shares/principal type = shares) with no put/call value.
  - Options, principal amounts (bonds) and non-ETF securities are excluded.
  - Shares of the same CUSIP across rows of one filing are summed.
- **F4. Value units.** Reported value is in thousands of dollars for filings made before 2023-01-03 and in dollars from that date on. It is normalized to USD and used only for checks (X2) and coverage (R11); position values for returns come from shares × our prices.
- **F5. Holdings over time.**
  - For a quarter-end `p`, `t(p)` is the last trading day on or before `p`, because quarter-ends can fall on weekends and holidays.
  - The holdings reported for `p` are held unchanged (as R6 units, `u = shares × c(t(p)) / TRI(t(p))`) from the close of `t(p)` until the next reported quarter-end.
  - The most recent report is carried forward to today.
  - For a window, `p0` is the latest quarter-end on or before `S`.
- **F6. ETF identification.**
  - Per §9. A CUSIP that can't be identified but whose issuer name or title of class contains "ETF" is treated as an unpriceable ETF (counts against R11).
  - All unidentified CUSIPs are summarized in Data issues.
- **F7. Required reports.**
  - Required periods are every quarter-end from `p0` through the latest quarter-end whose filing deadline (quarter-end + 45 days) has passed. A newer report the filer has already made is also used.
  - A filer missing any required report goes to Not enough data with "missing report for {period}".
- **F8. Listing threshold.**
  - A filer is listed only if its ETF value was at least $10,000,000 at every required quarter-end it reported. ETF value here means shares × close on `t(p)` for priced ETFs, plus the reported value of ETFs that can't be priced. That way a large filer with pricing problems lands in Not enough data (through R11) instead of disappearing.
  - Filers below that are not shown, because there are thousands of them.
  - The exception is filers on the famous list: they are always shown, ranked if they pass every rule and otherwise in Not enough data with the reason (for example "ETF holdings below $10M at 2024-06-30").
- **F9. Famous list (`famous_investors.json`).**
  - About 40 well-known investors whose firms file 13F.
  - Each entry: CIK, filer name exactly as on EDGAR, the person, their role stated accurately ("founder" where they no longer run it), EDGAR source URL and verification date.
- **F10. Shown per filer:** firm (plus person, for famous filers), total return, annualized, vs S&P, ETF value and number of ETFs at the latest report, ETF share of the book (F11), and top ETF by value. The details panel shows each quarter-end's ETF holdings with values, its ETF share of the book and links to the filings.
- **F11. ETF share of the book.** For a quarter-end, the reported value of its ETF rows divided by its book, the reported value of all its share rows (F3), with F2 applied to both. Both come from the filer's own information table, so a filing entered in the wrong units still gives the right share. Unknown (shown as —) if the book of any filing behind it is unknown. Shown for the latest quarter-end, with the average over the window's quarter-ends; it shows that the return is that of the ETF part of the book only.

### 8.3 Congress (badge: Estimate)

- **C1. Inputs.**
  - House: every index row with FilingType `P` from 2012 through the current year.
  - Senate: every Periodic Transaction Report from 2012-01-01.
  - Reports with no extractable text (scanned or paper) get status `scanned` and are listed in Data issues.
  - Reports that don't match the expected format get status `rejected` and are rejected whole; they are never half-read.
- **C2. Trade fields:** owner (self, SP, JT, DC), asset text, asset type code or label, ticker if present, transaction type (Purchase, Sale (Full), Sale (Partial), Exchange), transaction date, amount range.
- **C3. Amount ranges** are the reported brackets ($1,001–$15,000 … $25,000,001–$50,000,000).
  - The midpoint is `(low + high) / 2`. For example, $1,001–$15,000 → $8,000.50.
  - An open-ended bracket ("Over $X", including "Spouse/DC Over $X") uses `X + 1` as low, mid and high, and is marked open-ended in the details panel.
  - An exact amount (for example "$318.74", seen on real House reports for capital calls) is used as low, mid and high.
- **C4. Which trades are used.**
  - Trades whose asset resolves to an ETF (§9), including spouse, joint and dependent-child trades.
  - Option trades are excluded: House code `[OP]`, Senate type "Stock Option", or text describing calls or puts. They are counted in the details panel as excluded.
  - Exchange transactions are skipped and noted in Data issues.
- **C5. Simulation** (R6), in transaction-date order, then by report filing date, then by row order within the report. Trades are applied at the close of the last trading day on or before the transaction date. A transaction date later than the report's filing date goes to Data issues and is skipped.
  - **Purchase:** add `mid / TRI(d)` units.
  - **Sale (Partial):** remove `min(units held, mid / TRI(d))`.
  - **Sale (Full):** set units to 0.
  - A sale with no units held is ignored and noted as "sale of a position bought before the disclosure record" (the position predates 2012 or was never disclosed).
- **C6. Sensitivity.** The simulation is re-run with every trade at its low bound and at its high bound, and the two results are shown as a range next to the estimate. The page describes this as "the result if every trade were at the low end / high end of its range", not as a guaranteed bound.
- **C7. Eligibility.**
  - Ranked only if the invested fraction (R7) is at least 95%. Otherwise Not enough data with "invested on X% of trading days".
  - A member is not listed for a window if their simulated ETF holdings were empty for the whole window, or if they have no ETF trades at all.
  - Time in office limits both (C11).
- **C8. Member matching** (`members.py`). A unique match is required. Otherwise the person is shown by name and state only, with a Data issue. Filers who match no member of Congress, such as staff, are excluded and listed in Data issues.
  - Names are normalized before comparing: case, accents, punctuation, suffixes (Jr., Sr., II, III) and the titles the House index puts in name fields (Dr, MD, FACS, PhD, DVM; real data has "Dunn, MD, FACS") are removed. An apostrophe joins the parts around it: the index has "O'Rourke" where the member data has "O’Rourke".
  - **House:** candidates are the `rep` terms for the state and district in `StateDst` (at-large → district 0) whose window covers the filing date (term start ≤ filing date ≤ term end + 90 days). The filer's last name must appear among the words of the member's last name or full official name; real data has "Delaney" in the index for the member listed as "McClain Delaney".
  - **House, another district or a late report:** if no member of that district has the filer's name, the index may give a district the member does not sit for (real data lists Pete Sessions under TX32 in 2021, when he sat for TX17, and Rich McCormick under GA06 after redistricting moved him to GA07), or the filer left Congress and reports late (Sam Farr, five months after his last term). Then the `rep` terms of the whole state with term start ≤ filing date ≤ term end + 1 year are used: the filer's last name must appear among the words of the member's last name (not the full name, whose first names could match a surname), and the filer's first initial must match the member's first name, nickname or full official name, as for the Senate. Exactly one member must remain.
  - **Senate, late report:** if no term covers the filing date, a senator who left at most a year before it is taken when the last name and the first initial match and exactly one remains (real data: David Perdue reported four months after he left, and showed as a second person before this rule).
  - **Senate:** candidates are the `sen` terms covering the filing date (same window) whose last name equals the filer's last name. If more than one remains, keep those whose first initial matches the filer's. Real data shows first names can't be compared directly: eFD has "A. Mitchell" and "James Conley" where the member data has "Mitch" and "Jim"; two senators named Scott are told apart by "R" and "T".
- **C9. Duplicates and amendments.**
  - An amended report replaces the transactions of the report it amends, when it identifies that report.
  - Otherwise, identical transactions (same member, ticker, date, type, owner and range) are counted once.
  - Senate: an amendment's title is "Periodic Transaction Report for {date} (Amendment N)". It replaces the same senator's report with the same "for {date}"; a higher N replaces a lower one. This was confirmed on real data on 2026-10-08.
  - House: if no amendment link is found, the duplicate rule above applies.
  - House rows carry a filing status (New, Amended, Deleted), and trades are taken per member in filing order. A Deleted row is never counted. It removes the earlier trade with the same ticker, date, type, owner and range. If there is none, it removes the only earlier trade with the same ticker, date, type and owner. If that is not exactly one, nothing is removed and a Data issue is recorded. An Amended row replaces the only earlier trade with the same ticker, date, type and owner. If that is not exactly one, it counts as a new trade and a Data issue is recorded.
- **C10. Shown per member:** name, party-state, chamber, estimated total return with its low–high range, annualized, vs S&P, number of ETF trades used, and top ETF by current simulated value. The details panel lists every trade with a link to its report. A note explains the 45-day reporting lag.
- **C11. Time in office** (added 2026-10-16). Members report trades only while in office, so simulated holdings end
  when a member leaves; before this rule, Beto O'Rourke (left 2019) was ranked on two 2013 purchases never reported
  sold.
  - Times in office come from the congress-legislators terms, both chambers together (`members.service_periods`). A
    term that starts at most 14 days after the previous one ends continues it (terms end on Jan 3 and the next can
    start on Jan 6, as in 2015). A member who resigned has the actual last day as the term's end in that data.
  - Left office before the window starts: not listed for that window.
  - Left office during the window: Not enough data with "left office on {date}; trades after that are not reported".
  - Out of office for a time that ends inside the window (Suozzi: 2023-01-03 to 2024-02-13): Not enough data with "out
    of office from {date} to {date}; trades in that time are not reported".
  - Back in office after a break that ended before the window (Lummis: House to 2017, Senate from 2021): trades from
    the earlier time in office are not counted, and the details panel says how many.
  - Name-only members (C8) have no known time in office and are not limited.

## 9. Identifying ETFs and tickers

- **E1.** A security counts as an ETF only if both OpenFIGI reports security type `ETP` (US exchange) and Yahoo reports instrument type `ETF`. Securities whose name contains "ETN" or "Exchange Traded Note" are excluded.
- **E2. 13F.** CUSIP → OpenFIGI mapping (prefer exchange code `US`) → ticker → E1. Results, including "not found", are cached permanently in `cusip_map` with the raw response.
- **E3. Congress tickers** are taken in this order:
  1. a ticker in parentheses in the House asset text, or the Senate Ticker column;
  2. a token of 1–5 capital letters followed by the word "ETF" (for example "RSP ETF");
  3. an exact match, after normalizing case, punctuation and whitespace, of the asset name to exactly one ETF name in the Nasdaq Trader directory.

  Every candidate must pass E1. If no candidate passes but the text contains ETF-like words (ETF, Fund, Trust, Shares, or a fund-family name such as iShares, SPDR, Vanguard, Invesco, Schwab, ProShares, Direxion), the row goes to Data issues as "unmatched possible ETF".
- **E4. Reused tickers** (Congress, added 2026-10-16). A ticker can pass to a new ETF after the company that had it
  was renamed, bought or delisted: FB was Facebook's until 2022 and is a ProShares ETF's since 2025-06-26. A trade is
  identified by the ticker's current security, so such trades were counted as that ETF, without prices.
  - If an ETF's price history (from Yahoo, full history) starts after the transaction date of any trade reported
    under its ticker, a trade under that ticker counts only if its asset text names an ETF
    (`etf_check.looks_like_etf`: "ETF", "iShares", "SPDR", "Vanguard", "ProShares" and the other words listed there).
  - The others are not counted and go to Data issues as `reused-ticker`, naming the current ETF and its first price
    day. This covers later trades under the old ticker too (a 2025 "Meta Platforms (FB)" trade).
  - A trade whose text names an ETF stays counted, unpriced where the price history does not reach it (R11).

## 10. Live updates and first run

**Schedule** (America/New_York):

| Job | When |
|---|---|
| Live prices for all tracked ETFs + SPY (spark, 20 per request) | Every 5 min, Mon–Fri 09:30–16:00. If SPY's latest market time isn't today (a holiday), stop for the day |
| Completed-session closes and dividends (last 5 days) | 16:30 Mon–Fri |
| Full price-history re-fetch | For a ticker whenever a split appears in its recent data; for all tickers weekly (Sunday) |
| House index (current and previous year) + new reports | Hourly |
| Senate reports filed in the last 30 days | Every 6 hours |
| SEC current-filings feed (13F-HR, 13F-HR/A) | Hourly. On start, the daily index is read for every business day since the last day already covered (the end of the newest data set, or the last run) |
| Nasdaq Trader directory, congress-legislators data | Daily |
| Leaderboards | After any job that changed data (a data rebuild), and after each live-price run (a live rebuild). A live rebuild reads the stats that depend on filings only (`f13_period_stats`) instead of computing them |

**Page refresh:** `/api/status` is polled every 60 s. When its data version changes, the visible leaderboard is fetched again without reloading the page.

**First run (backfill):**
- All House and Senate trade reports since 2012.
- 13F data sets covering the periods back to the quarter-end on or before (today − 5 years).
- Ticker identification and full price histories for all ETFs involved and SPY.

Progress shows on the page. It is expected to take a few hours, mostly the 13F CUSIP lookup: an estimated 4 hours without an OpenFIGI key, minutes with one. Later runs are incremental.

**Performance:** recomputing all leaderboards on the full data set must take under 60 s.

## 11. Failure handling and Data issues

- Each source records its last success, last error and counters. When a source's last success is older than twice its schedule interval, its tab shows "stale since {time}"; everything else keeps working. Prices fall back to the last stored close.
- Each job writes inside one database transaction, so a failed job leaves the previous data in place.
- Network calls retry with exponential backoff, at most 5 attempts.
- **Data issues** (from `issues.py`) lists every skipped or doubtful item with its source, reason and original link:
  - scanned reports;
  - rejected reports;
  - unmatched assets and unmatched filers;
  - unidentified 13F CUSIPs (summarized);
  - failed cross-checks;
  - unpriceable ETFs;
  - ignored sales and exchanges;
  - transaction dates after the filing date.
  - Congress trades under a ticker that a later ETF reused (E4).
- Issues are deduplicated by (source, kind, reference) and marked resolved when a later run no longer produces them.
- All text from external documents is inserted into the page as text, never as HTML.

## 12. Cross-checks (run on every update)

- **X1. Dividends and splits.**
  - For each ETF, take the span from the later of 2012-01-01 and its first trading day, up to today.
  - At every month-end in that span, the cumulative R2 TRI ratio from the span start must be within 0.5% of the same ratio computed from Yahoo's adjusted closes.
  - A failing ETF is unpriceable (R11).
- **X2. 13F ticker matching.** For each position at each quarter-end `p`, the reported value must be within 10% of shares × the price as quoted on `t(p)`: our close there (split-adjusted) times the split ratios effective after `t(p)`, because a 13F counts the shares held at the quarter-end, before any later split. The same price values the position. A failing position is excluded as unpriceable. If more than half of a filing's positions fail, the whole filing is flagged as a likely unit error.
- **X3. ETF identity.** OpenFIGI and Yahoo must agree (E1). Disagreements are listed.
- **X4. Congress names.** Name matches must be unique (E3 step 3).
- **X5. Price sanity.** A daily close-to-close move of more than 50% with no split recorded is flagged for review. It is not excluded, because a few products really do move this much.
- **X6. Irregular dividends.** A dividend is flagged for review if the ticker paid at most 2 dividends in the last 5 years, or the dividend exceeds 5% of the prior close. Flag only. Verified data errors are fixed through `app/curated/dividend_corrections.json`, each entry citing an official fund document (for example, Yahoo's 2023-09-08 ARKK 'dividend', which ARK's annual report shows was never paid).

## 13. The page

Approved in the visual companion on 2026-10-08: the **trading-terminal** style with **expand-in-place** details. Mockups are saved in `.superpowers/brainstorm/`.

**Visual style:**
- Dark only, near-black background (`#0b0f14`), with all text and numbers in a monospaced font and tabular figures.
- Amber (`#f5a623`) marks the active tab, the active window, the selected row and the brand.
- Green for gains and positive "vs S&P", red for losses and negative "vs S&P". The sign (+/−) is always printed, so meaning never depends on color alone.
- Thin 1px separators and compact rows, so many rows fit on screen.

**Layout, top to bottom:**
- **Status bar:**
  - brand;
  - market status and time in ET ("● MKT OPEN 14:32 ET");
  - age of each source ("PX 2m · 13F 1h · HOUSE 1h · SENATE 1h"). A stale source turns amber ("SENATE stale 9h");
  - issues counter ("⚠ 37 issues"), which opens the Data issues list;
  - first-run progress while the backfill is running.
- **Control row:**
  - group tabs: MANAGERS, 13F, CONGRESS;
  - window keys: 1Y, 3Y, 5Y;
  - search box;
  - group filters: "Famous only" on 13F; House / Senate / Both on Congress.
- **Leaderboard table** at full width:
  - Columns: #, name (with firm, or party-state and chamber), group-specific columns (§8), RET (Congress: estimate plus low–high), /YR (3Y and 5Y only), vs SPX, and a badge (EXACT, APPROX, EST).
  - When the live data changes, a changed number flashes briefly. No other animation.
- **Expand in place:**
  - Clicking a row (or pressing Enter) opens a details panel directly beneath it. Several rows can be open at once; clicking again closes the panel.
  - The panel shows a line chart of the person's growth over the window against SPY (dashed), plus every fund, holding or trade used, with dates, sizes, prices, exclusions and links to the original documents.
- **Below the table:** collapsed "▸ NOT ENOUGH DATA (n)" with the reason for each person.
- **Methodology section** states §7 and §8 in plain language.
- **Footer:** not investment advice; personal, non-commercial use of congressional data; what the badges mean.

**Keyboard:**

| Key | Action |
|---|---|
| `/` | Focus search |
| ↑ ↓ | Move between rows |
| Enter | Open or close a row's details |
| 1, 3, 5 | Switch time window |
| Tab | Moves through every control |

**Other requirements:**
- Semantic table markup.
- On narrow screens (phone width), lower-priority columns hide and the table stays readable. Only the table scrolls sideways, never the page.

## 14. API (local only)

| Endpoint | Returns |
|---|---|
| `GET /` | the page |
| `GET /api/status` | data version, per-source status, market status, backfill progress, issues count |
| `GET /api/leaderboard?group={managers,investors,congress}&window={1y,3y,5y}` | ranked rows, not-enough-data rows, SPY benchmark, as-of time |
| `GET /api/person?group=…&id=…&window=…` | the details panel's data |
| `GET /api/issues/summary` | open data issues counted per source and kind |
| `GET /api/issues?source=&kind=&offset=&limit=` | the data-issues list, a page at a time (200 by default, at most 1000) |

The server binds to `127.0.0.1` only.

## 15. Configuration and setup

`run.sh` creates a Python virtual environment, installs pinned requirements and Playwright's Chromium, and on first run asks for:

1. **SEC contact email** (required): sent in the User-Agent of SEC requests, as the SEC requires. The user types it; nothing is pre-filled.
2. **OpenFIGI API key** (optional): leave blank to use the slower keyless limits.
3. **Senate terms:** shows the eFD use restrictions and enables the Senate source only if the user types `yes`.

The answers are stored in `config.toml`. The app then starts and opens `http://127.0.0.1:8765`. Ctrl+C stops it. A setting `senate_headed = true` runs the Senate browser visibly, in case headless Chrome is blocked.

## 16. Testing and acceptance

**Tests (pytest):**

1. **Return math** (`returns.py`, written test-first) with hand-worked cases:
   - dividends and the R2 index;
   - a split (for example SCHD's 3-for-1 split in October 2024 produces no artificial drop);
   - R3 live values, including an ex-dividend day;
   - R4 windows starting on weekends, holidays and Feb 29;
   - R6 with mid-window buys, partial and full sales, and empty periods;
   - R7, R8, R9, R10.
2. **Parsers on real saved documents:**
   - about 10 House reports: single and multi-page, ticker in parentheses and missing, spouse and joint trades, an option trade, an amendment, and one scanned report that must be rejected as `scanned`;
   - about 5 Senate reports, including a paper report;
   - 13F samples with option rows, principal-amount rows, both amendment types, and value units before and after 2023-01-03.
3. **Group rules:** each of M3–M6, F2–F8 and C3–C9 has at least one test.
4. **Curated lists:** every entry in `managers.json` and `famous_investors.json` has a source URL, quoted evidence (managers) and a verification date, and its tickers or CIKs exist.
5. **Independent spot-check:** 1Y, 3Y and 5Y market-price total returns of SPY, QQQ and ARKK at the latest month-end match each issuer's published figures within 0.1 percentage points per year.
6. **End-to-end in a real browser (Playwright),** on a fixture data set: every tab, window and filter works, details panels open and close, and every number on screen equals the API value.

**Acceptance criteria (the definition of done):**
- `./run.sh` on a fresh copy completes setup and opens the page.
- After the backfill, all three tabs show ranked lists for 1Y, 3Y and 5Y, and Data issues lists every skipped item with its reason.
- All tests pass.
- During market hours, prices update within 5 minutes and the open page reflects the change within 60 s without a reload.
- Manual audit: 5 random people per tab are checked by hand against their original documents. Any discrepancy is a bug and is fixed before completion.

## 17. Risks

| Risk | Mitigation |
|---|---|
| Yahoo changes or blocks its unofficial API | Last stored prices keep the page working; the provider is isolated in `prices.py` |
| Senate site blocks headless Chrome | The Senate source shows stale while House keeps working; `senate_headed` fallback |
| House or Senate change their report layout | Parsers reject unknown formats whole (C1), so nothing is misread; failures show in Data issues |
| OpenFIGI rate limits | Permanent cache; optional key |
| 13F data-set format changes | The parser validates expected columns and rejects the data set otherwise |
| Curated lists go stale (managers change) | Verification dates are shown in the details panel; entries older than 12 months are flagged in Data issues |
