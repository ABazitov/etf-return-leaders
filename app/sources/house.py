"""House periodic transaction reports (spec C1, C2)."""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path

from pypdf import PdfReader

from app.http import fetch
from app.sources.errors import ParseRejected

BASE = "https://disclosures-clerk.house.gov/public_disc"
UA = "ETF-Return-Leaders/1.0"


@dataclass(frozen=True)
class RawTrade:
    row: int
    owner: str
    asset_text: str
    asset_code: str
    tx_type: str
    tx_date: date
    notified: date
    amount_text: str
    description: str = ""   # old layout only: a description line that mentions options, calls or puts
    filing_status: str = "New"   # "New", "Amended" or "Deleted" (C9)


@dataclass(frozen=True)
class HouseReport:
    name: str
    status: str
    state_district: str
    filing_id: str
    trades: list = field(default_factory=list)


@dataclass(frozen=True)
class IndexRow:
    doc_id: str
    prefix: str
    first: str
    last: str
    suffix: str
    filing_type: str
    state_district: str
    year: int
    filed: date | None


def index_zip_url(year: int) -> str:
    return f"{BASE}/financial-pdfs/{year}FD.zip"


def pdf_url(year: int, doc_id: str) -> str:
    return f"{BASE}/ptr-pdfs/{year}/{doc_id}.pdf"


def parse_index_xml(xml: bytes) -> list[IndexRow]:
    out = []
    for m in ET.fromstring(xml):
        f = {c.tag: (c.text or "").strip() for c in m}
        out.append(IndexRow(f["DocID"], f["Prefix"], f["First"], f["Last"], f["Suffix"], f["FilingType"],
                            f["StateDst"], int(f["Year"]),
                            _d(f["FilingDate"]) if f["FilingDate"] else None))
    return out


_AMOUNT = r"(?:Spouse/DC\s+)?Over\s+\$[\d,]+|\$[\d,]+\s*-\s*\$[\d,]+|\$[\d,]+\.\d{2}"
_TAIL = (r"(?P<type>S \(partial\)|P|S|E)\s+(?P<tx>\d{2}/\d{2}/\d{4})\s+(?P<notif>\d{2}/\d{2}/\d{4})\s+"
         rf"(?P<amt>{_AMOUNT})")
_RECORD = re.compile(
    rf"^(?:\d{{4,}}\s+)?(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s*\[(?P<code>[A-Z0-9]{{2}})\]\s+{_TAIL}\s*$", re.S)
_SPLIT_RECORD = re.compile(  # a page break fell inside the asset name
    rf"^(?:\d{{4,}}\s+)?(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+{_TAIL}\s+(?P<asset2>.*?)\s*\[(?P<code>[A-Z0-9]{{2}})\]\s*$", re.S)
_SPLIT_AMOUNT = re.compile(  # a page break fell inside the amount range
    r"^(?:\d{4,}\s+)?(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+(?P<tx>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif>\d{2}/\d{2}/\d{4})\s+(?P<low>\$[\d,]+)\s*-\s+(?P<asset2>.*?)\s*\[(?P<code>[A-Z0-9]{2})\]\s+"
    r"(?P<high>\$[\d,]+)\s*$", re.S)
_CODE = re.compile(r"\[[A-Z0-9]{2}\]")
_OWNER_IN_ASSET = re.compile(r"(?:^|\s)(SP|JT|DC)\s")
_OWNER_START = re.compile(r"(?:SP|JT|DC)\s")
_DESC_LABEL = re.compile(r"[CD]\x00+:")  # "C" comment or "D" description label, e.g. "C\x00...: text"
_SUB_LABEL = re.compile(r"S\x00+ O\x00+:")  # "SUBHOLDING OF" label, whose text may wrap onto lines of its own
_DATE_PAIR = re.compile(r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}/\d{1,2}/\d{4}")
_STATUS_LABEL = re.compile(r"F\x00+ S\x00+:\s*(.*)")   # "FILING STATUS: New"
_STATUSES = {"new": "New", "amended": "Amended", "deleted": "Deleted"}


def _filing_status(value: str) -> str:
    status = _STATUSES.get(value.strip().lower())
    if status is None:
        raise ParseRejected(f"unknown filing status {value.strip()[:40]!r}")
    return status


def _is_detail_line(line: str) -> bool:
    # Labels like "F      S     : New" use a symbol font; pypdf returns NUL characters for it.
    return len(line) > 1 and line[0].isalpha() and line[1] == "\x00"


def _d(s: str) -> date:
    return datetime.strptime(s, "%m/%d/%Y").date()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def extract_text(path: Path) -> str:
    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


def _check_empty(buf: list[str]) -> None:
    if buf:
        raise ParseRejected(f"unparsed text before a transaction: {' '.join(buf)[:120]}")


def _holds_transaction_text(text: str) -> bool:
    return bool(_CODE.search(text) or _DATE_PAIR.search(text) or re.search(_AMOUNT, text))


def parse_text(text: str) -> HouseReport:
    if not text.strip():
        raise ParseRejected("scanned")
    head = {}
    for key in ("Name", "Status", "State/District"):
        m = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.M)
        if not m:
            raise ParseRejected(f"missing header field {key}")
        head[key] = m.group(1).strip()
    fid = re.search(r"Filing ID #(\d+)", text)
    if not fid:
        raise ParseRejected("missing Filing ID")
    trades: list[RawTrade] = []
    buf: list[str] = []
    in_table = in_header = in_desc = in_sub = False
    status_read = True       # the last parsed transaction already has its filing status line
    for line in text.split("\n"):
        s = line.strip()
        if s == "ID Owner Asset Transaction":       # table header, repeated on every page
            in_header = True
            continue
        if in_header:
            if s == "$200?":
                in_header, in_table = False, True
            continue
        if not in_table:
            continue
        if s.startswith("* For the complete list of asset type"):
            if (in_desc or in_sub) and not _holds_transaction_text(" ".join(buf)):
                buf = []                # a label's text that wrapped up to the table end is dropped,
            _check_empty(buf)           # unless it holds anything a transaction would
            in_table = False
            continue
        if s.startswith("Filing ID #"):              # page footer; a record may continue on the next page
            continue
        if _is_detail_line(line) or not s:
            if s and in_sub and not _holds_transaction_text(" ".join(buf)):
                buf = []    # likewise subholding text that wrapped up to the next detail line. Comment text there
            _check_empty(buf)    # still rejects: comments that wrap can also run into a next record without owner
            buf = []
            if _is_detail_line(line):
                in_desc, in_sub = bool(_DESC_LABEL.match(line)), bool(_SUB_LABEL.match(line))
                if st := _STATUS_LABEL.fullmatch(line):
                    if status_read:
                        raise ParseRejected(f"filing status line without its transaction: {s[:120]}")
                    trades[-1], status_read = replace(trades[-1], filing_status=_filing_status(st[1])), True
            continue
        buf.append(s)
        if in_desc:  # after a comment/description label, prose before the next owner-prefixed record is dropped
            for k in range(1, len(buf)):
                if _OWNER_START.match(buf[k]):
                    del buf[:k]
                    break
        joined = " ".join(buf)
        if m := _RECORD.match(joined):
            asset, amount = m["asset"], m["amt"]
        elif m := _SPLIT_RECORD.match(joined):
            asset, amount = m["asset"] + " " + m["asset2"], m["amt"]
        elif m := _SPLIT_AMOUNT.match(joined):
            asset, amount = m["asset"] + " " + m["asset2"], f"{m['low']} - {m['high']}"
        else:
            continue
        if _OWNER_IN_ASSET.search(asset):  # an owner token inside the asset means a stray line was merged in
            raise ParseRejected(f"unparsed text before a transaction: {asset[:120]}")
        trades.append(RawTrade(len(trades) + 1, m["owner"] or "self", _clean(asset), m["code"], m["type"],
                               _d(m["tx"]), _d(m["notif"]), _clean(amount)))
        buf, in_desc, in_sub, status_read = [], False, False, False
    _check_empty(buf)
    n_codes = len(_CODE.findall(text))
    if n_codes != len(trades):
        raise ParseRejected(f"found {n_codes} asset codes but parsed {len(trades)} transactions")
    return HouseReport(head["Name"], head["Status"], head["State/District"], fid.group(1), trades)


# ---------- the older layout (reports filed before 2022), read with pypdf's layout mode ----------
# pypdf's default text of these PDFs is mangled (small caps come out in odd letter case and words are glued
# together); its layout mode gives a clean column table. Letter case in labels and asset text is unreliable.

_L_DATE = r"\d{1,2}/\d{1,2}/\d{4}"
_L_DATE_PAIR = re.compile(rf"(?<![\d/]){_L_DATE}\s+{_L_DATE}(?![\d/])")   # transaction date, notification date
_L_HEADER = re.compile(r"\s*id\s+owner\s+asset\s+transaction\s+date\s+notification\s+amount(?:\s+cap\.)?\s*", re.I)
_L_HEADER_MORE = re.compile(r"\s*(?:(?:type|date|gains\s*>|\$200\?)(?:gfedcb?)?\s*)+", re.I)  # header's 2nd/3rd lines
_L_COLUMNS = ("id", "owner", "asset", "type", "date", "notification", "amount", "cap")
_L_TOLERANCE = 3                       # cell text starts within a few characters of its header word
_L_CELL = re.compile(r"\S+(?: \S+)*")  # cells are separated by two or more spaces
_L_ID_START = re.compile(r"\d+\s{2}")   # a transaction ID in the first column (amended or deleted rows)
_L_DETAIL = re.compile(r"(filing\s+status|subholding\s+of|description|location|comments)\s*:\s*(.*)", re.I)
_L_WRAPS = ("subholding of", "location", "description", "comments")   # detail labels whose text may wrap
_L_TABLE_END = "* For the complete list of asset type"
_L_TYPE = re.compile(r"([PSE])( \(partial\))?", re.I)
_L_OWNERS = ("SP", "JT", "DC")
_L_CHECKBOX = re.compile(r"gfedcb?")   # the "cap. gains > $200?" checkbox glyphs
_L_CODE = re.compile(r"\[[A-Za-z0-9]{2}\]")
_L_CODE_AT_END = re.compile(r"(.*?)\s*\[([A-Za-z0-9]{2})\]", re.S)
_L_TICKER = re.compile(r"\(([A-Za-z][A-Za-z0-9]{0,5}(?:[.\-][A-Za-z]{1,2})?)\)")
# Small caps make tickers come out in any case, even all lower case ("(goog)", "(baba)", "(all)" = Allstate), so a
# parenthesized group is a ticker unless it is a plain lower-case word like "(stock)" in "root9b ... (RTNb) (stock)".
_L_NOT_TICKERS = {"stock", "stocks", "bond", "bonds", "note", "notes", "class", "common", "preferred", "pfd", "shares",
                  "units", "fund", "funds", "trust", "call", "calls", "put", "puts", "option", "options", "warrant",
                  "warrants", "rights", "debt", "cash", "cusip", "ira", "roth", "old", "new"}
_L_SHIFTED_OWNER = re.compile(r"(?:sp|jt|dc)(?:\s|$)", re.I)
# "DC" also starts District of Columbia municipal bonds: "DC Rev Issues Prior to 5/20/10 NPR", "DC go boND 4.0% ...",
# "DC uNIV EDuC [gS]"
_L_DC_MUNI = re.compile(r"dc\s.*(?:\b(?:rev|go|bonds?|univ|educ|muni\w*|hsg|wtr|water|tax|gen|oblig\w*|auth)\b|%)", re.I)
_L_OPTION_WORDS = re.compile(r"\b(?:options?|calls?|puts?)\b", re.I)


def extract_layout_text(path: Path) -> str:
    return "\n".join(p.extract_text(extraction_mode="layout") or "" for p in PdfReader(str(path)).pages)


def _l_columns(header_line: str) -> dict[str, int]:
    starts = [m.start() for m in re.finditer(r"\S+", header_line)]
    return dict(zip(_L_COLUMNS, starts))


def _l_cells(line: str, cols: dict[str, int]) -> dict[str, list[str]]:
    """The line's text cells keyed by the column whose header word they start under."""
    order = sorted(cols.items(), key=lambda kv: kv[1])
    cells: dict[str, list[str]] = {}
    for m in _L_CELL.finditer(line):
        name = order[0][0]
        for col, pos in order:
            if m.start() >= pos - _L_TOLERANCE:
                name = col
        cells.setdefault(name, []).append(m.group())
    return cells


def _l_start(cells: dict[str, list[str]], line: str) -> dict:
    """A record's first line: the type, transaction date, notification date and amount columns are all filled."""
    def one(col: str) -> str:
        v = cells.get(col, [])
        if len(v) != 1:
            raise ParseRejected(f"unreadable transaction line: {line.strip()[:120]}")
        return v[0]

    if any(not x.isdigit() for x in cells.get("id", [])):
        raise ParseRejected(f"unreadable ID column: {line.strip()[:120]}")
    owner = cells.get("owner", [])
    if len(owner) > 1 or (owner and owner[0].upper() not in _L_OWNERS):
        raise ParseRejected(f"unknown owner {' '.join(owner)!r}: {line.strip()[:120]}")
    tm = _L_TYPE.fullmatch(one("type"))
    if not tm or (tm[2] and tm[1].upper() != "S"):
        raise ParseRejected(f"unknown transaction type {one('type')!r}: {line.strip()[:120]}")
    tx, notif = one("date"), one("notification")
    if not (re.fullmatch(_L_DATE, tx) and re.fullmatch(_L_DATE, notif)):
        raise ParseRejected(f"unreadable transaction dates: {line.strip()[:120]}")
    try:
        tx_date, notified = _d(tx), _d(notif)
    except ValueError:
        raise ParseRejected(f"unreadable transaction dates: {line.strip()[:120]}") from None
    if not cells.get("asset"):
        raise ParseRejected(f"transaction without an asset: {line.strip()[:120]}")
    first = cells["asset"][0]
    if not owner and _L_SHIFTED_OWNER.match(first) and not _L_DC_MUNI.match(first):
        raise ParseRejected(f"owner shifted into the asset column: {line.strip()[:120]}")
    if not all(_L_CHECKBOX.fullmatch(x) for x in cells.get("cap", [])):
        raise ParseRejected(f"unparsed text in the transaction table: {line.strip()[:120]}")
    return {"owner": owner[0].upper() if owner else "self", "asset": list(cells["asset"]),
            "type": tm[1].upper() + (" (partial)" if tm[2] else ""), "tx": tx_date, "notified": notified,
            "amount": [one("amount")], "description": [], "status": None}


def _l_amount(rec: dict) -> str:
    a = _clean(" ".join(rec["amount"]))
    return re.sub(r"^(?:(spouse/dc)\s+)?over(?= )", lambda m: "Spouse/DC Over" if m[1] else "Over", a, flags=re.I)


def _l_amount_open(rec: dict) -> bool:
    return not re.fullmatch(_AMOUNT, _l_amount(rec))


def _l_trade(row: int, rec: dict) -> RawTrade:
    asset, code = _clean(" ".join(rec["asset"])), ""
    if _L_CODE.search(asset):
        m = _L_CODE_AT_END.fullmatch(asset)
        if not m or _L_CODE.search(m[1]):
            raise ParseRejected(f"asset code not at the end of the asset: {asset[:120]}")
        asset, code = m[1], m[2].upper()
    asset = _L_TICKER.sub(lambda t: t[0] if t[1] in _L_NOT_TICKERS else f"({t[1].upper()})", asset)
    amount = _l_amount(rec)
    if not asset or not re.fullmatch(_AMOUNT, amount):
        raise ParseRejected(f"unreadable amount {amount!r} for {asset[:80]}")
    desc = _clean(" ".join(rec["description"]))
    if rec["status"] is None:
        raise ParseRejected(f"transaction without a filing status line: {asset[:120]}")
    return RawTrade(row, rec["owner"], asset, code, rec["type"], rec["tx"], rec["notified"], amount,
                    desc if _L_OPTION_WORDS.search(desc) else "", rec["status"] or "New")


def parse_layout_text(text: str) -> HouseReport:
    """Strict like parse_text (C1): every non-empty line of the table belongs to a record, is a known detail line
    (filing status, subholding, location, description, comments) or a page's repeated table header; and the number
    of transaction/notification date pairs anywhere in the text equals the number of records."""
    if not text.strip():
        raise ParseRejected("scanned")
    head = {}
    for key in ("Name", "Status", "State/District"):
        m = re.search(rf"^{re.escape(key)}:[ \t]*(\S.*?)\s*$", text, re.M | re.I)
        if not m:
            raise ParseRejected(f"missing header field {key}")
        head[key] = m.group(1)
    fid = re.search(r"Filing ID #(\d+)", text, re.I)
    if not fid:
        raise ParseRejected("missing Filing ID")
    recs: list[dict] = []
    cols: dict[str, int] | None = None
    in_table = after_header = False
    cont = None     # what an indented line may continue: "record" (asset/amount cells) or a list of free text
    for line in text.split("\n"):
        if _L_HEADER.fullmatch(line):              # the table header, repeated on every page
            cols, in_table, after_header = _l_columns(line), True, True
            continue
        if not in_table:
            continue
        s = line.strip()
        if after_header and (not s or _L_HEADER_MORE.fullmatch(line)):   # a page break: text may continue below
            continue
        after_header = False
        if not s:
            cont = None
            continue
        if s.startswith(_L_TABLE_END) or not (line[0].isspace() or _L_ID_START.match(line)):
            # the next section starts at the left margin; the date-pair count covers whatever follows
            in_table, cont = False, None
            continue
        d = _L_DETAIL.match(s)
        if d and abs(len(line) - len(line.lstrip()) - cols["asset"]) <= _L_TOLERANCE:
            if not recs:
                raise ParseRejected(f"unparsed text before a transaction: {s[:120]}")
            label = re.sub(r"\s+", " ", d[1]).lower()
            cont = None
            if label == "filing status":
                if recs[-1]["status"] is not None:
                    raise ParseRejected(f"second filing status for one transaction: {s[:120]}")
                recs[-1]["status"] = _filing_status(d[2])
            if label in _L_WRAPS:
                cont = recs[-1]["description"] if label == "description" else []
                cont.append(d[2])
            continue
        cells = _l_cells(line, cols)
        if any(_L_DETAIL.match(v) for vals in cells.values() for v in vals):
            raise ParseRejected(f"detail line outside the asset column: {s[:120]}")
        if cells.keys() & {"type", "date", "notification"}:
            recs.append(_l_start(cells, line))
            cont = "record"
            continue
        if isinstance(cont, list) and set(cells) == {"asset"}:
            cont.append(s)
            continue
        if cont == "record":
            rec = recs[-1]
            for col, vals in cells.items():
                if col == "asset":
                    rec["asset"] += vals
                elif col == "amount" and _l_amount_open(rec):
                    rec["amount"] += vals
                elif not (col == "cap" and all(_L_CHECKBOX.fullmatch(x) for x in vals)):
                    raise ParseRejected(f"unparsed text in the transaction table: {s[:120]}")
            continue
        raise ParseRejected(f"unparsed text in the transaction table: {s[:120]}")
    if cols is None:
        raise ParseRejected("missing transaction table")
    trades = [_l_trade(i + 1, r) for i, r in enumerate(recs)]
    n_pairs = len(_L_DATE_PAIR.findall(text))
    if n_pairs != len(trades):
        raise ParseRejected(f"found {n_pairs} date pairs but parsed {len(trades)} transactions")
    n_status = len(re.findall(r"filing\s+status\s*:", text, re.I))
    if n_status != len(trades):
        raise ParseRejected(f"found {n_status} filing status lines but parsed {len(trades)} transactions")
    return HouseReport(head["Name"], head["Status"], head["State/District"].upper(), fid.group(1), trades)


def parse_pdf(path: Path) -> HouseReport:
    """The current layout first; reports filed before 2022 fail its header check and are read in layout mode."""
    try:
        return parse_text(extract_text(path))
    except ParseRejected as e:
        if str(e) != "scanned" and not str(e).startswith("missing header field"):
            raise
        first_error = e
    layout = extract_layout_text(path)
    if not layout.strip():
        raise first_error
    return parse_layout_text(layout)


class HouseClient:
    def __init__(self, raw_dir: Path):
        self.raw_dir = Path(raw_dir) / "house"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def index(self, year: int) -> list[IndexRow]:
        body = fetch(index_zip_url(year), ua=UA, min_interval=0.5)
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            xml = z.read(f"{year}FD.xml")
        (self.raw_dir / f"{year}FD.xml").write_bytes(xml)
        return parse_index_xml(xml)

    def pdf(self, year: int, doc_id: str) -> Path:
        dest = self.raw_dir / str(year) / f"{doc_id}.pdf"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(fetch(pdf_url(year, doc_id), ua=UA, min_interval=0.5))
            tmp.rename(dest)
        return dest
