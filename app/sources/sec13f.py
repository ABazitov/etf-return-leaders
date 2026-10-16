"""SEC Form 13F holdings (spec F1-F4): bulk data sets, single-filing XML, daily index, current feed."""
from __future__ import annotations

import csv
import html
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

csv.field_size_limit(1 << 30)
UNITS_SWITCH = date(2023, 1, 3)   # filings made on/after this date report VALUE in dollars, before in $1000s
FORMS = {"13F-HR", "13F-HR/A"}
SEC = "https://www.sec.gov"


@dataclass(frozen=True)
class Filing:
    accession: str
    cik: str
    filer_name: str
    period: date
    filed: date
    form: str
    amendment_type: str


@dataclass(frozen=True)
class Holding:
    accession: str
    cusip: str
    shares: float
    value_usd: float
    issuer: str
    title: str


class DataSet:
    def __init__(self, path: Path):
        self.path = Path(path)

    def rows(self, name: str) -> Iterator[dict]:
        if self.path.suffix == ".zip":
            with zipfile.ZipFile(self.path) as z:
                member = next(n for n in z.namelist() if n.upper().endswith(f"{name.upper()}.TSV"))
                with z.open(member) as raw:
                    yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""),
                                              delimiter="\t", quoting=csv.QUOTE_NONE)
        else:
            with open(self.path / f"{name}.tsv", encoding="utf-8", errors="replace", newline="") as fh:
                yield from csv.DictReader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)


def _dsdate(s: str) -> date:
    return datetime.strptime(s.strip(), "%d-%b-%Y").date()


def read_filings(ds: DataSet) -> dict[str, Filing]:
    cover = {r["ACCESSION_NUMBER"]: r for r in ds.rows("COVERPAGE")}
    out = {}
    for r in ds.rows("SUBMISSION"):
        form = r["SUBMISSIONTYPE"].strip()
        if form not in FORMS:
            continue
        acc = r["ACCESSION_NUMBER"].strip()
        c = cover.get(acc, {})
        amend = (c.get("AMENDMENTTYPE") or "").strip().upper() if form == "13F-HR/A" else ""
        out[acc] = Filing(acc, str(int(r["CIK"])), (c.get("FILINGMANAGER_NAME") or "").strip(),
                          _dsdate(r["PERIODOFREPORT"]), _dsdate(r["FILING_DATE"]), form, amend)
    return out


def value_usd(raw: str, filed: date) -> float:
    v = float(raw or 0)
    return v * 1000 if filed < UNITS_SWITCH else v


def _keep(sh_type: str, put_call: str) -> bool:
    return sh_type.strip().upper() == "SH" and not put_call.strip()


def iter_share_rows(ds: DataSet, filings: dict[str, Filing]) -> Iterator[Holding]:
    for r in ds.rows("INFOTABLE"):
        f = filings.get(r["ACCESSION_NUMBER"])
        if f is None or not _keep(r["SSHPRNAMTTYPE"], r["PUTCALL"]):
            continue
        yield Holding(f.accession, r["CUSIP"].strip().upper(), float(r["SSHPRNAMT"] or 0),
                      value_usd(r["VALUE"], f.filed), r["NAMEOFISSUER"].strip(), r["TITLEOFCLASS"].strip())


def aggregate(holdings: Iterable[Holding]) -> dict[str, dict[str, Holding]]:
    out: dict[str, dict[str, Holding]] = defaultdict(dict)
    for h in holdings:
        prev = out[h.accession].get(h.cusip)
        out[h.accession][h.cusip] = h if prev is None else replace(
            prev, shares=prev.shares + h.shares, value_usd=prev.value_usd + h.value_usd)
    return dict(out)


def filing_totals(ds: DataSet, filings: dict[str, Filing]) -> dict[str, tuple[int, float]]:
    out = {}
    for r in ds.rows("SUMMARYPAGE"):
        f = filings.get(r["ACCESSION_NUMBER"])
        if f:
            out[f.accession] = (int(r["TABLEENTRYTOTAL"] or 0), value_usd(r["TABLEVALUETOTAL"], f.filed))
    return out


def _merge_holdings(p: Holding, h: Holding) -> Holding:
    return replace(p, shares=p.shares + h.shares, value_usd=p.value_usd + h.value_usd)


def effective_holdings(filings: Iterable[Filing], by_acc: dict[str, dict[str, Holding]], merge=_merge_holdings
                       ) -> tuple[dict[tuple[str, date], dict[str, Holding]], list[str]]:
    """F2: per (CIK, period), originals then amendments in filing order. `merge(earlier, added)` combines a NEW
    HOLDINGS row with an earlier row of the same CUSIP (callers holding other row types pass their own)."""
    groups: dict[tuple[str, date], list[Filing]] = defaultdict(list)
    for f in filings:
        groups[(f.cik, f.period)].append(f)
    out: dict[tuple[str, date], dict[str, Holding]] = {}
    unknown: list[str] = []
    for key, fs in groups.items():
        current: dict[str, Holding] | None = None
        for f in sorted(fs, key=lambda f: (f.filed, f.form != "13F-HR", f.accession)):
            rows = by_acc.get(f.accession, {})
            if f.form == "13F-HR" or f.amendment_type == "RESTATEMENT":
                current = dict(rows)
            elif f.amendment_type == "NEW HOLDINGS":
                current = dict(current or {})
                for cusip, h in rows.items():
                    p = current.get(cusip)
                    current[cusip] = h if p is None else merge(p, h)
            else:
                unknown.append(f.accession)
        if current is not None:
            out[key] = current
    return out, unknown


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str:
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_infotable_xml(xml: bytes, filing: Filing) -> list[Holding]:
    root = ET.fromstring(xml)
    out = []
    for row in root.iter():
        if _local(row.tag) != "infoTable":
            continue
        if not _keep(_child_text(row, "sshPrnamtType"), _child_text(row, "putCall")):
            continue
        out.append(Holding(filing.accession, _child_text(row, "cusip").upper(),
                           float(_child_text(row, "sshPrnamt") or 0),
                           value_usd(_child_text(row, "value"), filing.filed),
                           _child_text(row, "nameOfIssuer"), _child_text(row, "titleOfClass")))
    return out


def parse_primary_doc(xml: bytes, accession: str, cik: str, filed: date, form: str) -> Filing:
    root = ET.fromstring(xml)
    period = datetime.strptime(_child_text(root, "periodOfReport"), "%m-%d-%Y").date()
    filer = ""
    for el in root.iter():
        if _local(el.tag) == "filingManager":
            filer = _child_text(el, "name")
            break
    amend = _child_text(root, "amendmentType").upper() if form == "13F-HR/A" else ""
    return Filing(accession, str(int(cik)), filer, period, filed, form, amend)


@dataclass(frozen=True)
class IdxEntry:
    form: str
    company: str
    cik: str
    filed: date
    path: str


_IDX = re.compile(r"^(?P<form>\S.*?)\s{2,}(?P<company>\S.*?)\s{2,}(?P<cik>\d+)\s+(?P<filed>\d{8})\s+(?P<path>edgar/\S+)\s*$")


def parse_form_idx(text: str) -> list[IdxEntry]:
    out = []
    for line in text.splitlines():
        m = _IDX.match(line)
        if m:
            out.append(IdxEntry(m["form"], m["company"], str(int(m["cik"])),
                                datetime.strptime(m["filed"], "%Y%m%d").date(), m["path"]))
    return out


def accession_from_path(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".txt")


def parse_current_atom(xml: bytes) -> list[tuple[str, str, str, date]]:
    root = ET.fromstring(xml)
    out = []
    for entry in root:
        if _local(entry.tag) != "entry":
            continue
        title = _child_text(entry, "title")
        summary = html.unescape(_child_text(entry, "summary"))
        m_t = re.match(r"^(\S+) - .*\((\d{10})\) \(Filer\)$", title)
        m_a = re.search(r"accession-number=(\S+)$", _child_text(entry, "id"))
        m_f = re.search(r"Filed:</b> (\d{4}-\d{2}-\d{2})", summary)
        if m_t and m_a and m_f:
            out.append((m_t[1], str(int(m_t[2])), m_a[1], date.fromisoformat(m_f[1])))
    return out


def list_dataset_urls(page_html: str) -> list[str]:
    return [SEC + h if h.startswith("/") else h
            for h in re.findall(r'href="([^"]+_form13f\.zip)"', page_html)]


def dataset_range(url: str) -> tuple[date, date]:
    q = re.search(r"/(\d{4})q([1-4])_form13f\.zip$", url)
    if q:                      # old sets: filings made in that calendar quarter
        y, n = int(q[1]), int(q[2])
        start = date(y, 3 * n - 2, 1)
        end = (date(y + 1, 1, 1) if n == 4 else date(y, 3 * n + 1, 1)) - timedelta(days=1)
        return start, end
    m = re.search(r"(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f\.zip$", url)
    if not m:
        raise ValueError(f"unexpected data set name: {url}")
    p = lambda s: datetime.strptime(s, "%d%b%Y").date()
    return p(m[1]), p(m[2])
