"""Parse a bill-history XML file (ftp.legis.state.tx.us/bills/<s>/billhistory/...).

`parse_bill_history(xml_text, path)` returns (bill, actions):
    bill     one dict: session, bill, caption, authors, sponsors, subjects, agency_subjects, committees,
             last_update, last_action, and derived stage dates (filed / passed_house / passed_senate /
             sent_to_governor / signed / vetoed / effective)
    actions  one dict per <action>: bill, session, seq, action_number, chamber, action_date, description, comment
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime

BILL_ATTR_RE = re.compile(r"^(\d{2})\((R|\d)\)\s+([A-Z]+)\s+(\d+)$")
SUBJECT_RE = re.compile(r"^(.*?)\s*\(([A-Z0-9]{5})\)$")

# Action-number prefixes: H = House, S = Senate, E = executive (governor / enrollment).
# Matched on description text, which is stable across sessions; action numbers are not documented.
STAGES = {
    "filed": re.compile(r"^Filed$", re.I),
    "referred": re.compile(r"^Referred to ", re.I),
    # Enrolled = both chambers agreed on identical text (the Legislature's "passed" report counts these). Passing each
    # chamber is not enough: the chambers' versions may never be reconciled (no concurrence / no conference report).
    "enrolled": re.compile(r"^(Reported enrolled|Sent to the Governor)", re.I),
    "sent_to_governor": re.compile(r"^Sent to the Governor", re.I),
    "signed": re.compile(r"^Signed by the Governor", re.I),
    "vetoed": re.compile(r"^Vetoed by the Governor", re.I),
    "filed_without_signature": re.compile(r"^Filed without the Governor's signature", re.I),
    "effective": re.compile(r"^Effective ", re.I),
}


def _date(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _pipe_list(el) -> list[str]:
    if el is None or not (el.text or "").strip():
        return []
    return [x.strip() for x in el.text.split("|") if x.strip()]


def parse_bill_history(xml_text: str, path: str = "") -> tuple[dict, list[dict]]:
    root = ET.fromstring(xml_text.lstrip("﻿"))
    m = BILL_ATTR_RE.match(root.get("bill", "").strip())
    if not m:
        raise ValueError(f"unrecognized bill attribute {root.get('bill')!r} in {path}")
    leg, kind, btype, num = m.groups()
    session = f"{leg}{kind}"  # 89R, 891, 892 (matches the FTP folder names)
    bill = f"{btype}{int(num)}"

    subjects = []
    for s in root.findall("./subjects/subject"):
        sm = SUBJECT_RE.match((s.text or "").strip())
        subjects.append({"subject": sm.group(1) if sm else s.text, "code": sm.group(2) if sm else None})

    committees = {}
    for ch in ("house", "senate"):
        c = root.find(f"./committees/{ch}")
        if c is not None and c.get("name"):
            committees[ch] = {
                "name": c.get("name"), "status": c.get("status"),
                "aye": int(c.get("ayeVotes") or 0), "nay": int(c.get("nayVotes") or 0),
            }

    actions = []
    for i, a in enumerate(root.findall("./actions/action")):
        num_ = (a.findtext("actionNumber") or "").strip()
        actions.append({
            "session": session, "bill": bill, "seq": i,
            "action_number": num_, "chamber": num_[:1] or None,
            "action_date": _date(a.findtext("date")),
            "description": (a.findtext("description") or "").strip(),
            "comment": (a.findtext("comment") or "").strip() or None,
        })

    stage_dates: dict[str, str | None] = {}
    for stage, rx in STAGES.items():
        hit = next((x for x in actions if rx.search(x["description"])), None)
        stage_dates[stage] = hit["action_date"] if hit else None
    # Final passage per chamber = the action titled exactly "Passed" or "Passed as amended" on that chamber's action
    # numbers. Not "Record vote" (recorded votes also happen on amendments and failed motions) and not "Passed to
    # engrossment" / "Passed to 3rd reading" (second-reading steps). Checked against reports/passed.htm.
    final = re.compile(r"^Passed( as amended)?$", re.I)
    for ch, key in (("H", "passed_house"), ("S", "passed_senate")):
        hit = next((x for x in actions if x["chamber"] == ch and final.match(x["description"])), None)
        stage_dates[key] = hit["action_date"] if hit else None

    rec = {
        "source_path": path, "session": session, "bill": bill, "bill_type": btype, "bill_number": int(num),
        "caption": (root.findtext("caption") or "").strip(),
        "authors": _pipe_list(root.find("authors")), "sponsors": _pipe_list(root.find("sponsors")),
        "subjects": subjects,
        # Subject codes starting with V are agency subjects (e.g. INFORMATION RESOURCES, DEPARTMENT OF (V9827)).
        "agency_subjects": [s["subject"] for s in subjects if (s["code"] or "").startswith("V")],
        "committees": committees,
        "last_update": root.get("lastUpdate"),
        "last_action": (root.findtext("lastaction") or "").strip(),
        "n_actions": len(actions),
        **{f"{k}_date": v for k, v in stage_dates.items()},
    }
    rec["enacted"] = bool(rec["signed_date"] or rec["filed_without_signature_date"] or
                          (rec["effective_date"] and not rec["vetoed_date"]))
    return rec, actions


def parse_passed_report(html: str) -> dict:
    """reports/passed.htm -> {'reported_count': 3562, 'bills': ['HB2', ...]} for reconciliation."""
    m = re.search(r"Number of Bills:\s*([\d,]+)", html)
    return {
        "reported_count": int(m.group(1).replace(",", "")) if m else None,
        "bills": re.findall(r"Bill=([A-Z]+\d+)\"", html),
    }
