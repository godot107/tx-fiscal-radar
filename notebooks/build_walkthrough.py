"""Build and execute notebooks/walkthrough.ipynb: a plain-language tour of the results, run against the live
Azure SQL warehouse (the same numbers as the dashboard and the agent).

    .venv/bin/python notebooks/build_walkthrough.py
"""
from pathlib import Path

import nbformat
from nbclient import NotebookClient

HERE = Path(__file__).parent
md = nbformat.v4.new_markdown_cell
code = nbformat.v4.new_code_cell

cells = [
    md("""# Texas Fiscal Radar: walkthrough

**Question:** after the Texas Legislature passes its bills, which state agencies face new costs and duties, how large are
they relative to what each agency already spends, and what is coming in the 2027 session?

**Data:** the Legislature's own files (bill histories and Legislative Budget Board fiscal notes, from its public FTP site)
and the Comptroller's FY2024 spending by agency. Everything below is read from the project's Azure SQL warehouse,
which the Data Factory → Synapse → SQL pipeline fills. Nothing here is hand-typed.

*Words used:* a **fiscal note** is the Legislative Budget Board's estimate of what a bill would cost; **General Revenue
(GR)** is the state's main checking account; **enacted** means the bill became law; **FY2026-27** is the two-year budget
period that started September 1, 2025."""),
    code("""import sys, pandas as pd
sys.path.insert(0, "../src")
from txfr.common import connect_sql, load_env
load_env(); conn = connect_sql()

def q(sql):
    cur = conn.cursor(); cur.execute(sql)
    df = pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=[d[0] for d in cur.description])
    for c in df.columns:
        if df[c].dtype == object and df[c].dropna().map(lambda v: hasattr(v, "as_integer_ratio")).all():
            df[c] = df[c].astype(float)
    return df
pd.set_option("display.max_colwidth", 80); pd.set_option("display.float_format", "{:,.0f}".format)"""),
    md("""## 1. Can we trust the warehouse?

Before any analysis: the Legislature publishes its own count of bills that passed both chambers in 2025 (619 House +
594 Senate = 1,213). If the warehouse disagrees, every number after this is suspect."""),
    code("""q("SELECT passed_89r_hb_sb AS warehouse, passed_89r_hb_sb_official AS official, bills, fiscal_notes, "
  "ai_cost_rows, ai_rows_needing_review, last_load_utc FROM dw.v_data_quality")"""),
    md("""## 2. How far bills get

Most filed bills never become law. Knowing where they stop tells an agency where to spend analyst time."""),
    code("""q("SELECT session_code, bill_type, filed, referred, passed_one_chamber, passed_both, enacted, vetoed "
  "FROM dw.v_session_funnel WHERE bill_type IN ('HB','SB') ORDER BY session_code, bill_type")"""),
    md("![funnel](../docs/img/funnel_89r.png)"),
    md("""## 3. What the enacted bills cost, by agency

A fiscal note gives one cost for the whole bill. When a bill names one agency, the cost is that agency's (**direct**).
When it names several, the AI step reads the note's explanation and extracts the amounts it states per agency; a row
counts only if its quote is in the note, its number is in the quote and its agency is on the note's list
(**AI-attributed**). Costs on multi-agency bills with no such statement stay **shared**: counted, never guessed."""),
    code("""q("SELECT TOP 12 agency_name, enacted_bills, direct_cost_2yr, ai_cost_2yr, shared_bills, new_fte, fy2024_spend "
  "FROM dw.v_agency_exposure ORDER BY attributed_cost_2yr DESC")"""),
    md("![exposure](../docs/img/agency_exposure.png)"),
    md("""## 4. Readiness: who should prepare first

Raw dollars favour big agencies. Dividing the new yearly cost by the agency's FY2024 spending shows where the change
is large *for that agency*. FY2024 predates these laws, so this is a yardstick of size, not a budget."""),
    code("""q("SELECT TOP 12 agency_name, attributed_cost_2yr, fy2024_spend, load_pct_of_spend, new_fte "
  "FROM dw.v_agency_exposure WHERE fy2024_spend > 1000000 ORDER BY load_pct_of_spend DESC")"""),
    md("![readiness](../docs/img/readiness.png)"),
    md("""## 5. Costs that grow after the budget period

Some laws are cheap in 2026-27 and expensive later. These belong in the next budget request."""),
    code("""q("SELECT TOP 10 bill_id, LEFT(caption, 70) AS caption, cost_2yr, gr_cost_outyears FROM dw.v_bill_cost "
  "WHERE enacted = 1 AND grows_later = 1 ORDER BY gr_cost_outyears DESC")"""),
    md("""## 6. How much estimates move

Each version of a bill gets a new fiscal note. Bills whose estimate swung a lot deserve a contingency."""),
    code("""q("SELECT TOP 10 bill_id, status, first_version, first_cost_2yr, latest_version, latest_cost_2yr, change_pct "
  "FROM dw.v_estimate_drift WHERE session_code = '89R' ORDER BY ABS(change_2yr) DESC")"""),
    md("![drift](../docs/img/estimate_drift.png)"),
    md("""## 7. The 2027 session, live

Prefiling for the 2027 session opened on 2026-11-09. The pipeline picks up new filings every morning from the
Legislature's change list. Bills whose subjects name an agency are an early watchlist, before any fiscal note exists."""),
    code("""q("SELECT agency_subject, COUNT(DISTINCT bill_id) AS bills FROM dw.v_watchlist WHERE agency_subject IS NOT NULL "
  "GROUP BY agency_subject ORDER BY bills DESC OFFSET 0 ROWS FETCH NEXT 12 ROWS ONLY")"""),
    md("![pace](../docs/img/filing_pace.png)"),
    md("""## 8. What the AI step did, and how it was checked

Every AI row is checked against the note itself, not sampled. Rows that fail any check wait for a person on the
dashboard's *Trust & review* page."""),
    code("""q("SELECT COUNT(*) AS rows, SUM(CASE WHEN needs_review = 0 THEN 1 ELSE 0 END) AS verified, "
  "SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END) AS needs_review, COUNT(DISTINCT bill_id) AS bills, "
  "COUNT(DISTINCT CASE WHEN reconciles = 1 THEN bill_id END) AS bills_complete_split FROM dw.fact_agency_cost_ai")"""),
    md("""**Caveats.** Fiscal notes estimate one version of a bill; they are not appropriations. GR and all-funds figures
differ (each table says which). The AI split is a lower bound where a note's text doesn't itemise the whole cost."""),
]

nb = nbformat.v4.new_notebook(cells=cells, metadata={"kernelspec": {"name": "python3", "display_name": "Python 3"}})
NotebookClient(nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(HERE)}}).execute()
nbformat.write(nb, HERE / "walkthrough.ipynb")
print("wrote notebooks/walkthrough.ipynb (executed)")
