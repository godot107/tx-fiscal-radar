"""Power BI as code, from Linux (no Desktop): Azure SQL views → push dataset, and a PBIR report → Fabric API.

    python -m txfr.powerbi publish      # (re)create dataset "Texas Fiscal Radar" and push rows from the dw views
    python -m txfr.powerbi report       # write powerbi/report/ (PBIR) and publish it bound to the dataset
    python -m txfr.powerbi report --dry-run    # only write powerbi/report/

Pages (docs/DASHBOARD.md):
  1. Session pulse     filing pace 2027 vs 2025, funnel, committee bottlenecks
  2. Watchlist (90R)   bills filed so far that name an agency subject; hearing / fiscal-note flags
  3. Fiscal exposure   enacted-bill cost by agency, largest bills, costs that grow later
  4. Readiness         agencies ranked by new cost load vs FY2024 spending, implementation calendar
  5. Estimate drift    first vs latest fiscal-note estimate per bill
  6. Trust & review    reconciliation to the official count, freshness, AI rows waiting for review

Push datasets don't refresh from SQL by themselves: `publish` is the refresh (run after pl_daily; the
production design is an Import model on Azure SQL with scheduled refresh and RLS by agency, docs/ARCHITECTURE.md).

Auth: Power BI token for the work account from a separate az profile (same as insurance-docai):
    AZURE_CONFIG_DIR=~/.azure-powerbi az login --allow-no-subscriptions --tenant <tenant-id>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from txfr.common import ROOT

API = "https://api.powerbi.com/v1.0/myorg"
FABRIC = "https://api.fabric.microsoft.com/v1"
PBI_PROFILE = Path(os.environ.get("PBI_AZURE_CONFIG_DIR", Path.home() / ".azure-powerbi"))
OUT = ROOT / "powerbi" / "report"
BASE = ROOT / "powerbi" / "base"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
W, H = 1280, 720
DATASET = "Texas Fiscal Radar"
WORKSPACE = os.environ.get("PBI_WORKSPACE", "tx-fiscal-radar")

# dataset table name → SQL source
TABLES = {
    "funnel": "SELECT * FROM dw.v_session_funnel",
    "filing_pace": "SELECT * FROM dw.v_filing_pace WHERE day_of_prefiling BETWEEN 0 AND 90",
    "committee": "SELECT * FROM dw.v_committee_bottleneck",
    "bill_cost": """SELECT bill_id, session_code, bill, LEFT(caption, 200) AS caption, status, enacted, note_version, version_label,
                           cost_2yr, gr_2yr_cost, all_funds_cost_outyears, gr_cost_outyears, grows_later, fte_2027,
                           n_agencies, no_appropriation, cannot_be_determined, is_appropriations FROM dw.v_bill_cost""",
    "agency_exposure": "SELECT *, CAST(CASE WHEN fy2024_spend > 5000000 THEN 1 ELSE 0 END AS bit) AS rankable FROM dw.v_agency_exposure",
    "calendar": """SELECT bill_id, session_code, bill, LEFT(caption, 200) AS caption, agency_code, agency_name,
                          effective_or_signed, cost_2yr, fte_2027, duties FROM dw.v_implementation_calendar""",
    "drift": "SELECT bill_id, session_code, bill, LEFT(caption, 200) AS caption, status, n_versions, first_version, first_cost_2yr, latest_version, latest_cost_2yr, change_2yr, ABS(change_2yr) AS abs_change_2yr, change_pct FROM dw.v_estimate_drift",
    "watchlist": "SELECT bill_id, bill, LEFT(caption, 200) AS caption, filed_date, status, house_committee, senate_committee, agency_subject, has_fiscal_note, hearing_scheduled, last_action_date FROM dw.v_watchlist",
    "quality": "SELECT * FROM dw.v_data_quality",
    "ai_review": """SELECT x.bill_id, x.agency_code, a.agency_name, x.fiscal_year, -x.amount AS cost, x.cost_type,
                           LEFT(x.evidence, 400) AS evidence, x.reconciles, x.needs_review
                    FROM dw.fact_agency_cost_ai x LEFT JOIN dw.dim_agency a ON a.agency_code = x.agency_code""",
}
MEASURES = {
    "funnel": {
        "Bills filed 90R": "CALCULATE(SUM(funnel[filed]), funnel[session_code] = \"90R\", funnel[bill_type] IN {\"HB\", \"SB\"})",
        "Enacted 89R": "CALCULATE(SUM(funnel[enacted]), funnel[session_code] = \"89R\", funnel[bill_type] IN {\"HB\", \"SB\"})",
        "Vetoed 89R": "CALCULATE(SUM(funnel[vetoed]), funnel[session_code] = \"89R\", funnel[bill_type] IN {\"HB\", \"SB\"})",
        "Filed 89R": "CALCULATE(SUM(funnel[filed]), funnel[session_code] = \"89R\", funnel[bill_type] IN {\"HB\", \"SB\"})",
        "Enacted share 89R": "DIVIDE([Enacted 89R], [Filed 89R])",
    },
    "agency_exposure": {
        "Attributed cost 2026-27": "SUM(agency_exposure[attributed_cost_2yr])",
        "AI rows needing review": "SUM(agency_exposure[ai_rows_needing_review])",
    },
    "bill_cost": {
        "Enacted cost 2026-27": "CALCULATE(SUM(bill_cost[cost_2yr]), bill_cost[enacted] = TRUE(), bill_cost[is_appropriations] = 0) / 1e9",
        "Enacted bills with cost": "CALCULATE(COUNTROWS(bill_cost), bill_cost[enacted] = TRUE(), bill_cost[cost_2yr] > 0, bill_cost[is_appropriations] = 0)",
        "Appropriations bills 2026-27": "CALCULATE(SUM(bill_cost[gr_2yr_cost]), bill_cost[enacted] = TRUE(), bill_cost[is_appropriations] = 1) / 1e9",  # HB 500: its cost is in the GR impact
        "Cannot be determined": "CALCULATE(COUNTROWS(bill_cost), bill_cost[enacted] = TRUE(), bill_cost[cannot_be_determined] = TRUE())",
        "New FTEs 2027": "CALCULATE(SUM(bill_cost[fte_2027]), bill_cost[enacted] = TRUE(), bill_cost[cost_2yr] > 0, bill_cost[is_appropriations] = 0)",
    },
    "quality": {
        "Passed 89R (warehouse)": "SUM(quality[passed_89r_hb_sb])",
        "Passed 89R (official)": "SUM(quality[passed_89r_hb_sb_official])",
        "Reconciliation gap": "[Passed 89R (warehouse)] - [Passed 89R (official)]",
        "Last load (UTC)": "MAX(quality[last_load_utc])",
        "Failed ingests 7d": "SUM(quality[failed_ingests_7d])",
    },
    "watchlist": {"Watched bills": "DISTINCTCOUNT(watchlist[bill_id])"},
}
FORMATS = {"Attributed cost 2026-27": "$#,0,,.0M", "Enacted cost 2026-27": '\\$#,0.0"bn"', "Appropriations bills 2026-27": '\\$#,0.0"bn"',
           "Enacted share 89R": "0.0%", "New FTEs 2027": "#,0"}
COLUMN_FORMATS = {c: "$#,0" for c in ("cost_2yr", "gr_2yr_cost", "all_funds_cost_outyears", "gr_cost_outyears", "direct_cost_2yr",
                                      "ai_cost_2yr", "attributed_cost_2yr", "fy2024_spend", "first_cost_2yr", "latest_cost_2yr",
                                      "change_2yr", "abs_change_2yr", "cost")}
COLUMN_FORMATS.update({c: "yyyy-mm-dd" for c in ("effective_or_signed", "filed_date", "last_action_date")})
COLUMN_FORMATS.update({"load_pct_of_spend": "0.0", "change_pct": "0.0"})

# "Ask the agent" links: each bill row gets a URL that opens Fiscal Note Desk with the question filled in (the user
# still presses Ask: a link never spends model money by itself). Column `ask` is typed WebUrl in the dataset.
SESSION_YEAR = {"89R": "2025 regular session", "891": "2025 first called session", "892": "2025 second called session",
                "90R": "2027 session"}
ASK_QUESTIONS = {
    "watchlist": "What is {bill} ({session}) about, which agencies does it name, and does it have a fiscal note yet?",
    "bill_cost": "What does the latest fiscal note say {bill} ({session}) costs, and which agencies carry it?",
    "drift": "Why did the fiscal-note estimate for {bill} ({session}) change between versions?",
    "ai_review": "What does the fiscal note for {bill} ({session}) say {agency} would pay?",
}
ASK_COLUMN = "ask"
APP_PLACEHOLDER = "{{APP_URL}}"   # tracked report files carry this; the real host is substituted at upload only


def app_url() -> str:
    """Fiscal Note Desk base URL: APP_URL from .env locally, the Function App's own host when running in Azure."""
    url = os.environ.get("APP_URL") or (f"https://{os.environ['WEBSITE_HOSTNAME']}" if os.environ.get("WEBSITE_HOSTNAME") else "")
    return url.rstrip("/")


def bill_label(bill_id: str) -> tuple[str, str]:
    """'89R:SB2206' -> ('SB 2206', '2025 regular session')."""
    import re
    sess, _, bill = str(bill_id).partition(":")
    m = re.match(r"([A-Z]+)0*(\d+)$", bill)
    return (f"{m.group(1)} {m.group(2)}" if m else bill), SESSION_YEAR.get(sess, sess)


def ask_link(base: str, template: str, bill_id: str, agency: str | None = None) -> str:
    from urllib.parse import quote
    bill, sess = bill_label(bill_id)
    q = template.format(bill=bill, session=sess, agency=(agency or "the agency").title())
    return f"{base}/page?q={quote(q)}"   # an empty route maps to the function name: the page is /page


# ------------------------------------------------------------------ auth + REST
def token(resource: str) -> str:
    if os.environ.get("WEBSITE_INSTANCE_ID"):   # running in Azure (the Function App): its managed identity
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential().get_token(f"{resource.rstrip('/')}/.default").token
    env = dict(os.environ, AZURE_CONFIG_DIR=str(PBI_PROFILE))
    out = subprocess.run(["az", "account", "get-access-token", "--resource", resource, "--query", "accessToken", "-o", "tsv"],
                         capture_output=True, text=True, env=env)
    if out.returncode:
        raise SystemExit("No Power BI login. Run:\n  AZURE_CONFIG_DIR=~/.azure-powerbi az login --allow-no-subscriptions "
                         "--tenant <tenant-id>\n" + out.stderr[-300:])
    return out.stdout.strip()


def session(resource="https://analysis.windows.net/powerbi/api"):
    import requests
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token(resource)}"
    return s


def pbi_type(s) -> str:
    import pandas as pd
    if pd.api.types.is_bool_dtype(s):
        return "Boolean"
    if pd.api.types.is_integer_dtype(s):
        return "Int64"
    if pd.api.types.is_float_dtype(s):
        return "Double"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "DateTime"
    return "String"


def read_frames():
    import pandas as pd

    from txfr.common import connect_sql
    conn = connect_sql()
    frames = {}
    for t, q in TABLES.items():
        cur = conn.cursor()
        cur.execute(q)
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)
        for c in df.columns:
            vals = set(df[c].dropna().unique()) if df[c].dtype == object else None
            if vals is not None and vals and vals <= {True, False}:
                df[c] = df[c].astype("boolean")
            elif df[c].dtype == object and df[c].dropna().map(lambda v: hasattr(v, "as_integer_ratio")).all() and df[c].notna().any():
                df[c] = df[c].astype(float)   # SQL decimal → float
            elif df[c].dtype == object and df[c].dropna().map(lambda v: hasattr(v, "isoformat")).all() and df[c].notna().any():
                df[c] = pd.to_datetime(df[c])
        if t in ASK_QUESTIONS and app_url():
            df[ASK_COLUMN] = [ask_link(app_url(), ASK_QUESTIONS[t], b, a)
                              for b, a in zip(df["bill_id"], df.get("agency_name", [None] * len(df)))]
        frames[t] = df
    return frames


def workspace_id(s) -> str:
    g = s.get(f"{API}/groups", params={"$filter": f"name eq '{WORKSPACE}'"}).json()["value"]
    return g[0]["id"] if g else s.post(f"{API}/groups?workspaceV2=True", json={"name": WORKSPACE}).json()["id"]


def publish():
    s = session()
    gid = workspace_id(s)
    base = f"{API}/groups/{gid}"
    frames = read_frames()
    tables = [{"name": t, "columns": [{"name": c, "dataType": pbi_type(df[c]),
                                       **({"formatString": COLUMN_FORMATS[c]} if c in COLUMN_FORMATS else {}),
                                       **({"dataCategory": "WebUrl"} if c == ASK_COLUMN else {})} for c in df.columns],
               "measures": [{"name": m, "expression": e, **({"formatString": FORMATS[m]} if m in FORMATS else {})}
                            for m, e in MEASURES.get(t, {}).items()]} for t, df in frames.items()]
    existing = [d for d in s.get(f"{base}/datasets").json()["value"] if d["name"] == DATASET]
    if existing:
        did = existing[0]["id"]
        for tdef in tables:
            r = s.put(f"{base}/datasets/{did}/tables/{tdef['name']}", json=tdef)
            if not r.ok:
                raise SystemExit(f"update {tdef['name']}: {r.status_code} {r.text[:300]} (delete the dataset to change tables)")
    else:
        r = s.post(f"{base}/datasets", json={"name": DATASET, "defaultMode": "Push", "tables": tables})
        if not r.ok:
            raise SystemExit(f"create dataset failed: {r.status_code} {r.text[:500]}")
        did = r.json()["id"]
    for t, df in frames.items():
        rows = json.loads(df.to_json(orient="records", date_format="iso"))
        s.delete(f"{base}/datasets/{did}/tables/{t}/rows").raise_for_status()
        for i in range(0, len(rows), 5000):
            s.post(f"{base}/datasets/{did}/tables/{t}/rows", json={"rows": rows[i:i + 5000]}).raise_for_status()
        print(f"{t:16} {len(rows):6} rows")
    print(f"dataset: https://app.powerbi.com/groups/{gid}/datasets/{did}")
    return gid, did


# ------------------------------------------------------------------ PBIR helpers (same shapes as insurance-docai)
def vid(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def col(t, c):
    return {"Column": {"Expression": {"SourceRef": {"Entity": t}}, "Property": c}}


def meas(t, m):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": t}}, "Property": m}}


def agg(t, c, fn=0):   # 0 = Sum, 1 = Avg, 3 = Min, 4 = Max
    return {"Aggregation": {"Expression": col(t, c), "Function": fn}}


def proj(field, label=None):
    if "Measure" in field:
        t, p = field["Measure"]["Expression"]["SourceRef"]["Entity"], field["Measure"]["Property"]
        ref, native = f"{t}.{p}", p
    elif "Aggregation" in field:
        c = field["Aggregation"]["Expression"]["Column"]
        t, p = c["Expression"]["SourceRef"]["Entity"], c["Property"]
        fn = {0: "Sum", 1: "Avg", 3: "Min", 4: "Max"}[field["Aggregation"]["Function"]]
        ref, native = f"{fn}({t}.{p})", f"{fn} of {p}"
    else:
        t, p = field["Column"]["Expression"]["SourceRef"]["Entity"], field["Column"]["Property"]
        ref, native = f"{t}.{p}", p
    out = {"field": field, "queryRef": ref, "nativeQueryRef": native}
    if label:
        out["displayName"] = label
    return out


def lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def where(t, c, values):
    return {"Version": 2, "From": [{"Name": "x", "Entity": t, "Type": 0}],
            "Where": [{"Condition": {"In": {
                "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "x"}}, "Property": c}}],
                "Values": [[{"Literal": {"Value": v}}] for v in values]}}}]}


def filt(name, t, c, values):
    return {"name": name, "field": col(t, c), "type": "Categorical", "filter": where(t, c, values)}


def not_blank(name, t, c):
    cond = {"Not": {"Expression": {"In": {
        "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "x"}}, "Property": c}}],
        "Values": [[{"Literal": {"Value": "null"}}]]}}}}
    return {"name": name, "field": col(t, c), "type": "Advanced",
            "filter": {"Version": 2, "From": [{"Name": "x", "Entity": t, "Type": 0}], "Where": [{"Condition": cond}]}}


NO_TOTALS = {"total": [{"properties": {"totals": {"expr": {"Literal": {"Value": "false"}}}}}]}


def table(page, key, x, y, w, h, buckets, **kw):
    # WebUrl columns (the "Ask" links) show as a link icon rather than the long URL
    objects = {**NO_TOTALS, "values": [{"properties": {"urlIcon": lit("true")}}]}
    return visual(page, key, "tableEx", x, y, w, h, buckets, objects=objects, **kw)


def ask(t, label="Ask"):
    return (col(t, ASK_COLUMN), label)


def ask_button(page):
    """Top-right button that opens Fiscal Note Desk in a new tab (URL filled in at upload, see APP_PLACEHOLDER)."""
    d = {"id": "default"}   # buttons style per state: `show` has no selector, the styling sits on the default state
    on, off = lit("true"), lit("false")
    return visual(page, "ask_btn", "actionButton", 1100, 10, 160, 30, z=1000, objects={
        "icon": [{"properties": {"show": off}}],
        "text": [{"properties": {"show": on}},
                 {"properties": {"text": lit("'Ask the agent ↗'"), "fontSize": lit("11D"), "bold": on,
                                 "fontColor": {"solid": {"color": lit("'#FFFFFF'")}}}, "selector": d}],
        "fill": [{"properties": {"show": on}},
                 {"properties": {"fillColor": {"solid": {"color": lit("'#0F6CBD'")}}, "transparency": lit("0D")}, "selector": d}],
        "outline": [{"properties": {"show": off}}],
    }, link={"show": lit("true"), "type": lit("'WebUrl'"), "webUrl": lit(f"'{APP_PLACEHOLDER}/page'")})


def visual(page, key, vtype, x, y, w, h, buckets=None, title=None, filters=None, objects=None, z=0, sort=None, top=None,
           link=None):
    v = {"visualType": vtype}
    if buckets:
        v["query"] = {"queryState": {b: {"projections": [proj(*f) if isinstance(f, tuple) else proj(f) for f in fs]}
                                     for b, fs in buckets.items()}}
        if sort:
            v["query"]["sortDefinition"] = {"sort": [{"field": sort[0], "direction": sort[1]}], "isDefaultSort": False}
    if objects:
        v["objects"] = objects
    if title:
        v["visualContainerObjects"] = {"title": [{"properties": {"show": lit("true"), "text": lit(f"'{title}'")}}]}
    if link:
        v.setdefault("visualContainerObjects", {})["visualLink"] = [{"properties": link}]
    v["drillFilterOtherVisuals"] = True
    out = {"$schema": f"{SCHEMA}/visualContainer/2.14.0/schema.json", "name": vid(page, key),
           "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z}, "visual": v}
    if filters:
        out["filterConfig"] = {"filters": filters}
    return key, out


def text(page, key, x, y, w, h, runs):
    paras = [{"textRuns": [{"value": t, **({"textStyle": s} if s else {})} for t, s in p]} for p in runs]
    return visual(page, key, "textbox", x, y, w, h, objects={"general": [{"properties": {"paragraphs": paras}}]})


def card(page, key, x, y, m, w=200, h=100):
    return visual(page, key, "card", x, y, w, h, {"Values": [m]}, objects={"categoryLabels": [{"properties": {"fontSize": lit("10D")}}],
                                                                       "labels": [{"properties": {"labelDisplayUnits": lit("1D")}}]})


def slicer(page, key, x, y, w, h, c, title):
    return visual(page, key, "slicer", x, y, w, h, {"Values": [c]}, title=title,
                  objects={"data": [{"properties": {"mode": lit("'Dropdown'")}}]})


H1 = {"fontWeight": "bold", "fontSize": "20pt"}
NOTE = {"fontSize": "10pt", "color": "#605E5C"}


def header(p, title, note):
    return text(p, "title", 20, 8, 1240, 64, [[(title, H1)], [(note, NOTE)]])


# ------------------------------------------------------------------ pages
def page_pulse():
    p = "pulse"
    return p, "Session pulse", [
        header(p, "Session pulse", "2027 regular session (90R) prefiling opened 2026-11-09. Same-day comparison with the 2025 session (89R) "
                                   "uses days since prefiling opened. Source: Texas Legislature FTP bill histories."),
        card(p, "filed90", 20, 80, meas("funnel", "Bills filed 90R")),
        card(p, "filed89", 230, 80, meas("funnel", "Filed 89R")),
        card(p, "enacted89", 440, 80, meas("funnel", "Enacted 89R")),
        card(p, "share89", 650, 80, meas("funnel", "Enacted share 89R")),
        card(p, "vetoed89", 860, 80, meas("funnel", "Vetoed 89R")),
        visual(p, "pace", "lineChart", 20, 195, 620, 510,
               {"Category": [(col("filing_pace", "day_of_prefiling"), "Days since prefiling opened")],
                "Y": [(agg("filing_pace", "filed_cumulative", 4), "Bills filed (cumulative)")],
                "Series": [(col("filing_pace", "session_code"), "Session")]}, title="Filing pace: 2027 vs 2025"),
        visual(p, "bottleneck", "clusteredBarChart", 650, 195, 610, 510,
               {"Category": [(col("committee", "committee"), "Committee")],
                "Y": [(agg("committee", "still_in_committee"), "Still in committee"), (agg("committee", "enacted"), "Enacted")]},
               title="2025: where referred bills stopped (top committees)", sort=(agg("committee", "still_in_committee"), "Descending"),
               filters=[filt("c_session", "committee", "session_code", ["'89R'"])]),
    ]


def page_watchlist():
    p = "watch"
    w = "watchlist"
    return p, "Watchlist (2027)", [
        header(p, "Watchlist: 2027 bills naming an agency", "Pick an agency subject. Flags show whether a hearing is scheduled or a fiscal "
                                                         "note exists yet; refreshed daily from the Legislature's change manifest."),
        slicer(p, "agency", 20, 80, 400, 90, col(w, "agency_subject"), "Agency subject"),
        card(p, "n", 440, 80, meas(w, "Watched bills")),
        table(p, "bills", 20, 185, 1240, 520,
               {"Values": [(col(w, "bill"), "Bill"), ask(w), (col(w, "caption"), "Caption"), (col(w, "filed_date"), "Filed"),
                           (col(w, "status"), "Status"), (col(w, "agency_subject"), "Agency subject"),
                           (col(w, "hearing_scheduled"), "Hearing?"), (col(w, "has_fiscal_note"), "Fiscal note?"),
                           (col(w, "last_action_date"), "Last action")]}, title="Bills (newest filing first)",
              sort=(col(w, "filed_date"), "Descending")),
    ]


def page_exposure():
    p = "exposure"
    e, b = "agency_exposure", "bill_cost"
    return p, "Fiscal exposure", [
        header(p, "Fiscal exposure of enacted bills (89th Legislature, 2025)", "Costs for FY2026-27 from the latest fiscal note. Agency bars: "
               "bills naming one agency (direct) + per-agency amounts the AI extracted and that passed every check. Shared costs are not spread."),
        card(p, "cost", 20, 80, meas(b, "Enacted cost 2026-27"), w=240),
        card(p, "nbills", 270, 80, meas(b, "Enacted bills with cost"), w=240),
        card(p, "fte", 520, 80, meas(b, "New FTEs 2027"), w=240),
        card(p, "undet", 770, 80, meas(b, "Cannot be determined"), w=240),
        card(p, "approps", 1020, 80, meas(b, "Appropriations bills 2026-27"), w=240),
        visual(p, "by_agency", "barChart", 20, 195, 560, 510,
               {"Category": [(col(e, "agency_name"), "Agency")],
                "Y": [(agg(e, "direct_cost_2yr"), "Direct"), (agg(e, "ai_cost_2yr"), "AI-attributed (checked)")],
                "Tooltips": [(agg(e, "attributed_cost_2yr"), "Total")]},
               title="New 2026-27 cost by agency", sort=(agg(e, "attributed_cost_2yr"), "Descending")),
        table(p, "largest", 590, 195, 670, 300,
               {"Values": [(col(b, "bill_id"), "Bill"), ask(b), (col(b, "caption"), "Caption"), (agg(b, "cost_2yr"), "Cost 2026-27"),
                           (agg(b, "fte_2027"), "FTEs"), (agg(b, "n_agencies"), "Agencies")]},
               title="Largest enacted bills", sort=(agg(b, "cost_2yr"), "Descending"),
               filters=[filt("enacted", b, "enacted", ["true"]), filt("noapp", b, "is_appropriations", ["0L"])]),
        table(p, "later", 590, 505, 670, 200,
               {"Values": [(col(b, "bill_id"), "Bill"), ask(b), (col(b, "caption"), "Caption"), (agg(b, "gr_cost_outyears"), "GR cost FY2028-30")]},
               title="Costs that grow after the biennium", sort=(agg(b, "gr_cost_outyears"), "Descending"),
               filters=[filt("enacted2", b, "enacted", ["true"]), filt("grows", b, "grows_later", ["1L"])]),
    ]


def page_readiness():
    p = "ready"
    e, c = "agency_exposure", "calendar"
    return p, "Readiness", [
        header(p, "Readiness: which agencies to prepare first", "New cost load = attributed cost per year ÷ FY2024 spending (Comptroller). "
               "FY2024 predates these laws; it is a size yardstick, not a budget."),
        table(p, "rank", 20, 80, 700, 625,
               {"Values": [(col(e, "agency_name"), "Agency"), (agg(e, "load_pct_of_spend"), "Load % of spend"),
                           (agg(e, "attributed_cost_2yr"), "Cost 2026-27"), (agg(e, "fy2024_spend"), "FY2024 spend"),
                           (agg(e, "new_fte"), "New FTEs"), (agg(e, "shared_bills"), "Shared bills"),
                           (agg(e, "ai_rows_needing_review"), "AI rows to review")]},
               title="Agencies ranked by new cost load (FY2024 spend over $5M)", sort=(agg(e, "load_pct_of_spend"), "Descending"),
               filters=[filt("rankable", e, "rankable", ["true"])]),
        slicer(p, "agency", 730, 80, 530, 80, col(c, "agency_name"), "Agency"),
        table(p, "cal", 730, 170, 530, 535,
               {"Values": [(col(c, "effective_or_signed"), "Effective"), (col(c, "bill_id"), "Bill"), (col(c, "caption"), "Caption"),
                           (agg(c, "cost_2yr"), "Cost 2026-27"), (col(c, "duties"), "Duties (AI)")]},
               title="Implementation calendar", sort=(col(c, "effective_or_signed"), "Ascending"),
               filters=[not_blank("dated", c, "effective_or_signed")]),
    ]


def page_drift():
    p = "drift"
    d = "drift"
    return p, "Estimate drift", [
        header(p, "Estimate drift: how fiscal notes change as a bill moves", "First vs latest two-year General Revenue estimate for bills with "
               "more than one fiscal-note version. Volatile estimates deserve a contingency."),
        visual(p, "scatter", "scatterChart", 20, 80, 600, 625,
               {"Category": [(col(d, "bill_id"), "Bill")], "X": [(agg(d, "first_cost_2yr"), "First estimate")],
                "Y": [(agg(d, "latest_cost_2yr"), "Latest estimate")]}, title="First vs latest estimate, enacted bills (2026-27 cost)",
               filters=[filt("enacted_s", d, "status", ["'enacted'"])]),
        table(p, "table", 630, 80, 630, 625,
               {"Values": [(col(d, "bill_id"), "Bill"), ask(d), (col(d, "status"), "Status"), (col(d, "first_version"), "First"),
                           (agg(d, "first_cost_2yr"), "First cost"), (col(d, "latest_version"), "Latest"),
                           (agg(d, "latest_cost_2yr"), "Latest cost"), (agg(d, "abs_change_2yr"), "Size of change"),
                           (agg(d, "change_pct", 1), "Change %")]},
               title="Biggest changes, enacted bills", sort=(agg(d, "abs_change_2yr"), "Descending"),
               filters=[filt("enacted_t", d, "status", ["'enacted'"])]),
    ]


def page_trust():
    p = "trust"
    q, r = "quality", "ai_review"
    return p, "Trust & review", [
        header(p, "Trust & review", "Every load is reconciled to the Legislature's own count of passed bills; AI-extracted amounts that fail a "
                                    "check (quote not in the note, agency not listed, totals don't add up) wait here for a person."),
        card(p, "wh", 20, 80, meas(q, "Passed 89R (warehouse)"), w=240),
        card(p, "off", 270, 80, meas(q, "Passed 89R (official)"), w=240),
        card(p, "gap", 520, 80, meas(q, "Reconciliation gap"), w=240),
        card(p, "load", 770, 80, meas(q, "Last load (UTC)"), w=240),
        card(p, "fail", 1020, 80, meas(q, "Failed ingests 7d"), w=240),
        table(p, "queue", 20, 195, 1240, 510,
               {"Values": [(col(r, "bill_id"), "Bill"), ask(r), (col(r, "agency_name"), "Agency"), (col(r, "fiscal_year"), "FY"),
                           (agg(r, "cost"), "Cost"), (col(r, "cost_type"), "Type"), (col(r, "reconciles"), "Adds up?"),
                           (col(r, "evidence"), "Evidence (quote from the note)")]},
               title="AI extractions waiting for review", filters=[filt("nr", r, "needs_review", ["true"])]),
    ]


PAGES = [page_pulse, page_watchlist, page_exposure, page_readiness, page_drift, page_trust]


def write_definition(dataset_id: str, workspace: str):
    if OUT.exists():
        shutil.rmtree(OUT)
    shutil.copytree(BASE / "StaticResources", OUT / "StaticResources")
    (OUT / "definition").mkdir(parents=True)
    for f in ("report.json", "version.json"):
        shutil.copy(BASE / "definition" / f, OUT / "definition" / f)
    shutil.copy(BASE / ".platform", OUT / ".platform")
    (OUT / "definition.pbir").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byConnection": {"connectionString":
            f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace};initial catalog=\"{DATASET}\";"
            f"integrated security=ClaimsToken;semanticmodelid={dataset_id}"}}}, indent=2))
    pdir = OUT / "definition" / "pages"
    order, n = [], 0
    for fn in PAGES:
        key, name, visuals = fn()
        visuals = [*visuals, ask_button(key)]
        pid = vid("page", key)
        order.append(pid)
        (pdir / pid / "visuals").mkdir(parents=True)
        (pdir / pid / "page.json").write_text(json.dumps({"$schema": f"{SCHEMA}/page/2.1.0/schema.json", "name": pid,
                                                          "displayName": name, "displayOption": "FitToPage",
                                                          "height": H, "width": W}, indent=2))
        for _, v in visuals:
            (pdir / pid / "visuals" / v["name"]).mkdir()
            (pdir / pid / "visuals" / v["name"] / "visual.json").write_text(json.dumps(v, indent=2))
            n += 1
    (pdir / "pages.json").write_text(json.dumps({"$schema": f"{SCHEMA}/pagesMetadata/1.1.0/schema.json",
                                                 "pageOrder": order, "activePageName": order[0]}, indent=2))
    return len(order), n


def wait(s, resp):
    if resp.status_code == 200:
        return resp.json() if resp.content else None
    if resp.status_code != 202:
        raise SystemExit(f"Fabric API {resp.status_code}: {resp.text[:500]}")
    op = resp.headers["x-ms-operation-id"]
    for _ in range(60):
        time.sleep(int(resp.headers.get("Retry-After", 5)))
        st = s.get(f"{FABRIC}/operations/{op}").json()
        if st["status"] == "Succeeded":
            r = s.get(f"{FABRIC}/operations/{op}/result")
            return r.json() if r.ok and r.content else None
        if st["status"] == "Failed":
            raise SystemExit(f"Fabric operation failed: {json.dumps(st)[:800]}")
    raise SystemExit("Fabric operation timed out")


def report(dry_run: bool):
    if dry_run:
        n_pages, n = write_definition("00000000-0000-0000-0000-000000000000", WORKSPACE)
        print(f"wrote {OUT.relative_to(ROOT)}: {n_pages} pages, {n} visuals (dry run, placeholder dataset id)")
        return
    s = session()
    gid = workspace_id(s)
    did = next(d["id"] for d in s.get(f"{API}/groups/{gid}/datasets").json()["value"] if d["name"] == DATASET)
    rep = next((r for r in s.get(f"{API}/groups/{gid}/reports").json()["value"] if r["name"] == DATASET), None)
    n_pages, n = write_definition(did, WORKSPACE)
    fab = session("https://api.fabric.microsoft.com")
    base_url = app_url()
    if not base_url:
        raise SystemExit("APP_URL not set (.env): needed for the 'Ask the agent' links")
    parts = [{"path": p.relative_to(OUT).as_posix(), "payloadType": "InlineBase64",
              "payload": base64.b64encode(p.read_bytes().replace(APP_PLACEHOLDER.encode(), base_url.encode())).decode()}
             for p in sorted(OUT.rglob("*")) if p.is_file()]
    body = {"definition": {"parts": parts}}
    if rep:
        wait(fab, fab.post(f"{FABRIC}/workspaces/{gid}/reports/{rep['id']}/updateDefinition", json=body))
        rid = rep["id"]
    else:
        res = wait(fab, fab.post(f"{FABRIC}/workspaces/{gid}/reports", json={"displayName": DATASET, **body}))
        rid = res["id"] if res else "?"
    print(f"published {n_pages} pages / {n} visuals: https://app.powerbi.com/groups/{gid}/reports/{rid}")


def main():
    from txfr.common import load_env
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["publish", "report"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    publish() if a.cmd == "publish" else report(a.dry_run)


if __name__ == "__main__":
    main()
