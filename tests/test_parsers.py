"""Parser tests on real files pulled from ftp.legis.state.tx.us (89th Regular Session, 2025)."""
from pathlib import Path

import pytest

from txfr.bill_history import parse_bill_history, parse_passed_report
from txfr.fiscal_note import parse_agencies, parse_fiscal_note, parse_money

FX = Path(__file__).parent / "fixtures"


def note(name):
    html = (FX / "fiscalnotes" / name).read_text(encoding="utf-8", errors="replace")
    return parse_fiscal_note(html, f"/bills/89R/fiscalnotes/html/house_bills/HB00001_HB00099/{name}")


def hist(name):
    return parse_bill_history((FX / "billhistory" / name).read_text(encoding="utf-8"), name)


@pytest.mark.parametrize("text,want", [
    ("($15,697,853)", -15697853.0), ("$1,234", 1234.0), ("$0", 0.0), ("", None), ("n/a", None),
])
def test_parse_money(text, want):
    assert parse_money(text) == want


def test_agency_names_with_commas_split_on_codes():
    got = parse_agencies("116 Sunset Advisory Commission, 212 Office of Court Administration, "
                         "Texas Judicial Council, 300 Trusteed Programs Within the Office of the Governor")
    assert [a["agency_code"] for a in got] == ["116", "212", "300"]
    assert got[1]["agency_name"] == "Office of Court Administration, Texas Judicial Council"


def test_hb2_enrolled_note_matches_printed_two_year_figure():
    r = note("HB00002F.htm")
    assert (r["session"], r["bill"], r["version"]) == ("89R", "HB2", "F")
    assert r["gr_2yr_net"] == -8_381_506_751  # "a negative impact of ($8,381,506,751)"
    assert r["note_date"] == "2025-05-24"
    # the two-year figure is FY2026 + FY2027 of the five-year GR table
    yrs = {x["fiscal_year"]: x["amount"] for x in r["gr_5yr"]}
    assert yrs[2026] + yrs[2027] == r["gr_2yr_net"]
    assert r["agencies"] == []  # enrolled versions often leave Source Agencies blank
    assert r["warnings"] == []


def test_hb10_introduced_note():
    r = note("HB00010I.htm")
    assert r["gr_2yr_net"] == -15_697_853 and r["gr_2yr_kind"] == "negative"
    assert r["version_label"] == "As Introduced" and r["note_date"] == "2025-03-11"
    assert len(r["agencies"]) == 5 and r["agencies"][-1]["agency_code"] == "313"
    fte = [x for x in r["all_funds_5yr"] if "value" in x]
    assert {x["value"] for x in fte} == {35.5}


def test_hb2_introduced_has_agencies():
    codes = {a["agency_code"] for a in note("HB00002I.htm")["agencies"]}
    assert {"701", "304", "323"} <= codes and len(codes) == 9


def test_hb2_history_enacted():
    b, actions = hist("HB2.xml")
    assert b["enacted"] and b["signed_date"] == "2025-06-20" and b["enrolled_date"] is not None
    # third-reading passage, not the second-reading record votes on 4/16 ("Passed to engrossment as amended")
    assert b["passed_house_date"] == "2025-04-17" and b["passed_senate_date"] == "2025-05-23"
    assert b["committees"]["house"]["name"] == "Public Education"
    assert len(actions) == b["n_actions"] == 151
    assert [a["seq"] for a in actions] == list(range(151))


def test_hb1_died_in_committee():
    b, _ = hist("HB1.xml")
    assert not b["enacted"] and b["passed_house_date"] is None
    assert b["committees"]["house"]["status"] == "In committee"


def test_agency_subjects():
    b, _ = hist("HB10.xml")
    assert "SECRETARY OF STATE" in b["agency_subjects"]


def test_passed_report_header():
    r = parse_passed_report((FX / "reports" / "passed_89R_head.htm").read_text())
    assert r["reported_count"] == 3562 and r["bills"][0] == "HB2"


def test_conference_committee_version_and_indeterminate_cost():
    html = (FX / "fiscalnotes" / "SB00005C.htm").read_text(encoding="utf-8", errors="replace")
    r = parse_fiscal_note(html, "/bills/892/fiscalnotes/html/senate_bills/SB00001_SB00099/SB00005C.htm")
    assert (r["session"], r["bill"], r["version"]) == ("892", "SB5", "C")
    assert r["version_label"] == "Conference Committee Report"
    h = (FX / "fiscalnotes" / "HB00071I.htm").read_text(encoding="utf-8", errors="replace")
    r2 = parse_fiscal_note(h, "/bills/892/fiscalnotes/html/house_bills/HB00001_HB00099/HB00071I.htm")
    assert r2["gr_2yr_kind"] == "indeterminate" and r2["gr_2yr_net"] is None and r2["cannot_be_determined"]
    assert r2["warnings"] == []


@pytest.mark.parametrize("summary,kind", [
    ("There would be an indeterminate cost to the state dependent on the grant program.", "indeterminate"),
    ("As a result, there would be an indeterminate reduction in transfers to the state.", "indeterminate"),
    ("It is unknown if the provisions of the bill would apply to navigation services.", "indeterminate"),
    ("No significant fiscal implication to the State is anticipated.", "no_significant"),
    ("a negative impact of ($1,234) through the biennium", "negative"),
    ("The bill amends the Government Code.", "unknown"),
])
def test_two_year_kinds(summary, kind):
    from txfr.fiscal_note import _two_year
    assert _two_year(summary)[1] == kind
