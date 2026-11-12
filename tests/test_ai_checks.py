"""Deterministic checks on AI extractions (no model call)."""
from txfr.ai_extract import build_prompt, check_extraction

NOTE = {"session": "89R", "bill": "HB10", "version": "I",
        "summary": "a negative impact of ($15,697,853) through the biennium.",
        "fiscal_analysis": "The Office would be established within the OOG.",
        "methodology": "OOG estimates costs of $7,907,650 in fiscal year 2026 and $7,790,203 in fiscal year 2027.",
        "technology": None, "local_gov": None}
AG = [{"agency_code": "300", "agency_name": "Trusteed Programs Within the Office of the Governor"}]
YEARS = [{"table_name": "gr", "fiscal_year": 2026, "line_item": "GR", "amount": -7_907_650.0},
         {"table_name": "gr", "fiscal_year": 2027, "line_item": "GR", "amount": -7_790_203.0}]
EV = "OOG estimates costs of $7,907,650 in fiscal year 2026 and $7,790,203 in fiscal year 2027."


def row(code="300", fy=2026, amt=7_907_650.0, ev=EV, direction="cost"):
    return {"agency_code": code, "fiscal_year": fy, "amount": amt, "direction": direction, "cost_type": "staff", "evidence": ev}


def test_good_extraction_passes():
    r = check_extraction({"agency_costs": [row(), row(fy=2027, amt=7_790_203.0)], "mandates": []}, NOTE, AG, YEARS)
    assert r["reconciles"] and not any(x["needs_review"] for x in r["agency_costs"])


def test_invented_evidence_is_flagged():
    r = check_extraction({"agency_costs": [row(ev="The governor will hire 40 people at a cost of $8 million."),
                                           row(fy=2027, amt=7_790_203.0)], "mandates": []}, NOTE, AG, YEARS)
    assert not r["agency_costs"][0]["evidence_grounded"] and r["agency_costs"][0]["needs_review"]


def test_unlisted_agency_is_flagged():
    r = check_extraction({"agency_costs": [row(code="701")], "mandates": []}, NOTE, AG, YEARS)
    assert not r["agency_costs"][0]["agency_listed"] and r["agency_costs"][0]["needs_review"]


def test_partial_split_is_verified_but_not_complete():
    # true statement from the note, but it does not account for the whole bill: kept (lower bound), flagged incomplete
    note = {**NOTE, "methodology": NOTE["methodology"] + " OOG would spend $1,000,000 in fiscal year 2026 on IT."}
    r = check_extraction({"agency_costs": [row(amt=1_000_000.0, ev="OOG would spend $1,000,000 in fiscal year 2026 on IT."),
                                           row(fy=2027, amt=7_790_203.0)], "mandates": []}, note, AG, YEARS)
    assert not r["reconciles"] and not r["agency_costs"][0]["needs_review"]


def test_prompt_contains_agencies_and_tables():
    p = build_prompt(NOTE, AG, YEARS)
    assert "300 Trusteed Programs" in p and "FY2026 | GR | -7907650.0" in p


def test_model_arithmetic_is_flagged():
    # the quote is real but the amount (sum of two printed figures) is not written in it
    r = check_extraction({"agency_costs": [row(amt=15_697_853.0), row(fy=2027, amt=7_790_203.0)], "mandates": []}, NOTE, AG, YEARS)
    assert not r["agency_costs"][0]["amount_stated"] and r["agency_costs"][0]["needs_review"]


def test_sign_comes_from_direction():
    r = check_extraction({"agency_costs": [row(), row(fy=2027, amt=7_790_203.0)], "mandates": []}, NOTE, AG, YEARS)
    assert r["agency_costs"][0]["signed_amount"] == -7_907_650.0 and r["reconciles"]


def test_amounts_in_text():
    from txfr.ai_extract import amounts_in
    assert amounts_in("costs of $12.7 million and $3,500 and $1.2 billion") == [12_700_000.0, 3_500.0, 1_200_000_000.0]


def test_table_line_evidence_counts_only_for_single_agency_bills():
    ev = "FY2026 | GR | -7907650.0"
    one = check_extraction({"agency_costs": [row(ev=ev), row(fy=2027, amt=7_790_203.0)], "mandates": []}, NOTE, AG, YEARS)
    assert one["agency_costs"][0]["from_table"] and not one["agency_costs"][0]["needs_review"]
    two = check_extraction({"agency_costs": [row(ev=ev)], "mandates": []}, NOTE,
                           AG + [{"agency_code": "212", "agency_name": "Office of Court Administration"}], YEARS)
    assert two["agency_costs"][0]["needs_review"]


def test_agency_aliases_and_naming():
    from txfr.ai_extract import agency_aliases, agency_named
    assert "tea" in agency_aliases("Texas Education Agency")
    assert "oca" in agency_aliases("Office of Court Administration, Texas Judicial Council")
    assert "hhsc" in agency_aliases("Health and Human Services Commission")
    assert agency_named("Based on information provided by the Comptroller of Public Accounts", "Comptroller of Public Accounts")
    assert agency_named("TEA estimates the cost would be $12.7 million", "Texas Education Agency")
    assert not agency_named("The estimated cost to the Foundation School Program (FSP) is $193.5 million",
                            "Comptroller of Public Accounts")


def test_multi_agency_row_must_name_its_agency():
    ag2 = AG + [{"agency_code": "212", "agency_name": "Office of Court Administration"}]
    r = check_extraction({"agency_costs": [row(), row(fy=2027, amt=7_790_203.0)], "mandates": []}, NOTE, ag2, YEARS)
    # quote says "OOG estimates ..." and agency 300 is "Trusteed Programs Within the Office of the Governor" → not named
    assert not r["agency_costs"][0]["agency_named"] and r["agency_costs"][0]["needs_review"]
