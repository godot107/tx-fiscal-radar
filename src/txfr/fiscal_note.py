"""Parse a Legislative Budget Board fiscal note (HTML from ftp.legis.state.tx.us/bills/<s>/fiscalnotes/html).

Stdlib only (html.parser + re), so the same code runs locally, in tests, and inside a Synapse Spark job
without installing packages on the pool.

Output of `parse_fiscal_note(html, path)` is one dict:
    session, bill, chamber, bill_type, bill_number, version, version_label, note_date,
    gr_2yr_net        signed dollars (negative = cost to General Revenue), None if not stated
    gr_2yr_kind       'negative' | 'positive' | 'zero' | 'no_significant' | 'indeterminate' | 'unknown'
    agencies          [{'agency_code': '701', 'agency_name': 'Texas Education Agency'}, ...]
    gr_5yr            [{'fiscal_year': 2026, 'amount': -4033729029.0}, ...]
    all_funds_5yr     [{'fiscal_year': 2026, 'column': '<header>', 'amount': ... or 'value': ...}, ...]
    warnings          list of strings (anything the parser could not read; drives the quarantine table)
"""
from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser

# File name: HB00010I.htm -> chamber H, type B, number 10, version I (Introduced).
FILE_RE = re.compile(r"(?P<chamber>[HS])(?P<type>B|JR|CR|R)(?P<num>\d{5})(?P<ver>[IHSEACF])\.htm$", re.I)
VERSION_CODES = {
    "I": "introduced", "H": "house committee report", "S": "senate committee report",
    "E": "engrossed", "A": "as passed 2nd house", "C": "conference committee report", "F": "enrolled",
}
SESSION_RE = re.compile(r"/(?:bills|legis)/(\d{2}[R\d])/", re.I)  # FTP path or bronze lake path
MONEY_RE = re.compile(r"\(?\$?\s*-?[\d,]+(?:\.\d+)?\)?")
SECTIONS = {
    "summary": "divSumStmt", "fiscal_analysis": "divFiscalAnalysis", "methodology": "divMethodology",
    "technology": "divTechnology", "local_gov": "divLocalGov",
}
AGENCY_RE = re.compile(r"(\d{3})\s+(.+?)(?=,\s*\d{3}\s|\s*$)", re.S)


def parse_money(text: str) -> float | None:
    """'($15,697,853)' -> -15697853.0; '$1,234' -> 1234.0; '$0' -> 0.0; anything else -> None."""
    t = text.strip()
    if not t or not re.search(r"\d", t):
        return None
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-") or t.startswith("$-")
    digits = re.sub(r"[^\d.]", "", t)
    try:
        v = float(digits)
    except ValueError:
        return None
    return -v if neg else v


class _Collector(HTMLParser):
    """Collects text per element id and the rows of every <table id=...>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_by_id: dict[str, list[str]] = {}
        self.all_text: list[str] = []
        self.tables: dict[str, list[list[str]]] = {}
        self._id_stack: list[str | None] = []  # parallel to the open-tag stack, for div/td ids
        self._table: str | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("id"):
            self._table = a["id"]
            self.tables[self._table] = []
        elif tag == "tr" and self._table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        if tag == "div":
            self._id_stack.append(a.get("id"))
        elif tag == "br":
            self._data("\n")

    def handle_endtag(self, tag):
        if tag == "div" and self._id_stack:
            self._id_stack.pop()
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table:
            if self._row:
                self.tables[self._table].append(self._row)
            self._row = None
        elif tag == "table":
            self._table = None

    def handle_data(self, data):
        self._data(data)

    def _data(self, data):
        self.all_text.append(data)
        if self._cell is not None:
            self._cell.append(data)
        for i in self._id_stack:
            if i:
                self.text_by_id.setdefault(i, []).append(data)

    def text(self, id_: str) -> str:
        return " ".join("".join(self.text_by_id.get(id_, [])).split())


def _two_year(summary: str) -> tuple[float | None, str]:
    s = summary.lower()
    if "no significant fiscal implication" in s or re.search(r"\bno fiscal implication", s):
        return 0.0, "no_significant"
    # "The fiscal implications of the bill cannot be determined because ..." - a real outcome, not a parse failure
    if re.search(r"fiscal implications? of the bill cannot be (determined|estimated)", s):
        return None, "indeterminate"
    m = re.search(r"a (negative|positive) impact of\s*(\(?\$[\d,().]+\)?)", summary, re.I)
    if m:
        v = parse_money(m.group(2))
        if v is None:
            return None, "unknown"
        v = -abs(v) if m.group(1).lower() == "negative" else abs(v)
        return v, m.group(1).lower()
    if re.search(r"impact of \$0\b", summary, re.I):
        return 0.0, "zero"
    # No dollar headline, and the note says why: "indeterminate cost", "indeterminate reduction in transfers",
    # "it is unknown if ...", "cannot be estimated". Seen in 173 notes of the 89th Legislature.
    if re.search(r"indeterminate|cannot be (determined|estimated)|\bunknown\b|unable to (determine|estimate)", s):
        return None, "indeterminate"
    return None, "unknown"


def _five_year(rows: list[list[str]]) -> list[dict]:
    """GR table: header row then [fiscal_year, amount] rows."""
    out = []
    for r in rows:
        if len(r) >= 2 and re.fullmatch(r"20\d\d", r[0]):
            v = parse_money(r[1])
            if v is not None:
                out.append({"fiscal_year": int(r[0]), "amount": v})
    return out


def _all_funds(rows: list[list[str]]) -> list[dict]:
    """All-funds table: header row names the columns (fund / FTE change); one row per fiscal year."""
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        if not r or not re.fullmatch(r"20\d\d", r[0]):
            continue
        for col, cell in zip(header[1:], r[1:]):
            item = {"fiscal_year": int(r[0]), "column": col}
            if "employee" in col.lower():  # FTE count, not dollars
                try:
                    item["value"] = float(cell.replace(",", ""))
                except ValueError:
                    continue
            else:
                v = parse_money(cell)
                if v is None:
                    continue
                item["amount"] = v
            out.append(item)
    return out


def parse_agencies(text: str) -> list[dict]:
    """'116 Sunset Advisory Commission, 212 Office of Court Administration, Texas Judicial Council, ...'
    Agency names can contain commas, so split on the 3-digit agency codes, not on commas."""
    return [
        {"agency_code": m.group(1), "agency_name": m.group(2).strip().rstrip(",").strip()}
        for m in AGENCY_RE.finditer(text.strip())
    ]


def parse_fiscal_note(html: str, path: str) -> dict:
    warnings: list[str] = []
    name = path.rsplit("/", 1)[-1]
    fm = FILE_RE.search(name)
    sm = SESSION_RE.search(path)
    rec: dict = {
        "source_path": path,
        "session": sm.group(1).upper() if sm else None,
        "chamber": None, "bill_type": None, "bill_number": None, "bill": None, "version": None,
    }
    if fm:
        rec.update(
            chamber=fm["chamber"].upper(), bill_type=fm["type"].upper(), bill_number=int(fm["num"]),
            bill=f"{fm['chamber'].upper()}{fm['type'].upper()}{int(fm['num'])}", version=fm["ver"].upper(),
        )
    else:
        warnings.append(f"unrecognized file name {name}")
    if not rec["session"]:
        warnings.append("session not found in path")

    c = _Collector()
    c.feed(html)
    c.close()

    in_re = c.text("divEditInRe")
    m = re.search(r",\s*(As [A-Za-z0-9 ]+?|Conference Committee Report)\s*$", in_re)
    rec["version_label"] = m.group(1) if m else None
    # The heading div is closed early by unbalanced </div> tags, so search the whole text after the title.
    full = " ".join("".join(c.all_text).split())
    dm = re.search(r"FISCAL NOTE.*?([A-Z][a-z]+ \d{1,2}, \d{4})", full)
    rec["note_date"] = datetime.strptime(dm.group(1), "%B %d, %Y").date().isoformat() if dm else None
    if not rec["note_date"]:
        warnings.append("note date not found")

    summary = c.text("divSumStmt")
    rec["gr_2yr_net"], rec["gr_2yr_kind"] = _two_year(summary)
    if rec["gr_2yr_kind"] == "unknown":
        warnings.append("two-year GR impact not parsed")

    rec["agencies"] = parse_agencies(c.text("divEditAgySource"))
    rec["gr_5yr"] = _five_year(c.tables.get("GR_TABLE1", []))
    rec["all_funds_5yr"] = _all_funds(c.tables.get("FiscImpOneTbl1", []))
    # Free-text sections: the input to the AI extraction phase (per-agency costs, mandate types).
    for key, div in SECTIONS.items():
        rec[key] = c.text(div) or None
    rec["no_appropriation"] = bool(re.search(r"would make no appropriation", c.text("divNoAppr") + " " + summary, re.I))
    body = " ".join(filter(None, [summary, rec["fiscal_analysis"], rec["methodology"]]))
    rec["cannot_be_determined"] = rec["gr_2yr_kind"] == "indeterminate" or bool(re.search(r"cannot be (determined|estimated)", body, re.I))
    rec["local_gov_significant"] = (None if not rec["local_gov"] else
                                    not re.search(r"no significant fiscal implication", rec["local_gov"], re.I))
    if rec["gr_2yr_kind"] in ("negative", "positive") and not rec["gr_5yr"]:
        warnings.append("five-year GR table missing")
    rec["warnings"] = warnings
    return rec
