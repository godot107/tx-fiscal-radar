"""Phase 6b (part 1): build the Azure AI Search index the Foundry agent is grounded in.

One index, `tx-session`, three kinds of documents (field `doc_type`):
    bill_card     one per bill: status, committees, dates, fiscal-note headline, agencies, AI mandates
    agency_card   one per agency: enacted-bill cost load (direct + AI-attributed), new FTEs, FY2024 spending
    note_section  fiscal-note prose (fiscal analysis / methodology / technology / local government), chunked,
                  for enacted bills with a fiscal impact and every 90R note
Numbers in the cards come from the Azure SQL star schema (the same numbers as the dashboard), so the agent quotes
the warehouse instead of doing arithmetic. Every document carries `source_url` (the fiscal note / bill history
file on the Legislature FTP) for citations.

Free tier: 50 MB, 3 indexes, keyword (BM25) search. Auth: Entra ID (the service has local auth disabled).

    python -m txfr.search_index            # (re)create the index and upload everything
"""
from __future__ import annotations

import io
import os
import re

INDEX = "tx-session"
FTP = "ftp://ftp.legis.state.tx.us"


def money(v):
    if v is None or v != v or str(v) in ("<NA>", "nan"):  # None / NaN / pd.NA
        return "n/a"
    v = float(v)
    sign = "-" if v < 0 else ""
    v = abs(v)
    for unit, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= d:
            return f"{sign}${v / d:,.2f}{unit}"
    return f"{sign}${v:,.0f}"


def ftp_url(path: str | None) -> str | None:
    """abfss://bronze@acct.dfs.core.windows.net/legis/89R/fiscalnotes/house_bills/G/HB00002F.htm →
    ftp://ftp.legis.state.tx.us/bills/89R/fiscalnotes/html/house_bills/G/HB00002F.htm"""
    if not isinstance(path, str) or not path:
        return None
    m = re.search(r"/legis/(\w+)/(billhistory|fiscalnotes)/(.+)$", path)
    if not m:
        return None
    session, kind, rest = m.groups()
    sub = "fiscalnotes/html" if kind == "fiscalnotes" else "billhistory"
    return f"{FTP}/bills/{session}/{sub}/{rest}".replace(" ", "%20")


def chunks(text: str, size: int = 2000, overlap: int = 200):
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    i = 0
    while i < len(text):
        yield text[i:i + size]
        i += size - overlap


def key(*parts) -> str:
    return re.sub(r"[^A-Za-z0-9_\-=]", "_", "-".join(str(p) for p in parts))


def sql_frame(conn, q):
    import pandas as pd
    cur = conn.cursor()
    cur.execute(q)
    cols = [d[0] for d in cur.description]
    return pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)


def build_docs(conn, lake) -> list[dict]:
    from txfr.ai_extract import read_gold

    bills = sql_frame(conn, """
        SELECT b.bill_id, b.session_code, b.bill, b.caption, b.status, b.house_committee, b.house_committee_status,
               b.senate_committee, b.senate_committee_status, b.filed_date, b.passed_house_date, b.passed_senate_date,
               b.signed_date, b.vetoed_date, b.effective_date, b.authors, b.source_path,
               n.version, n.version_label, n.gr_2yr_net, n.gr_2yr_kind, n.no_appropriation, n.cannot_be_determined,
               n.source_path AS note_path
        FROM dw.dim_bill b LEFT JOIN dw.fact_fiscal_note n ON n.bill_id = b.bill_id AND n.is_latest = 1
        WHERE b.is_current = 1""")
    agencies = sql_frame(conn, """SELECT ba.bill_id, a.agency_code, a.agency_name
                                  FROM dw.bridge_bill_agency ba JOIN dw.dim_agency a ON a.agency_code = ba.agency_code""")
    mandates = sql_frame(conn, "SELECT bill_id, mandate_type, agency_code FROM dw.fact_bill_mandate")
    fte = sql_frame(conn, """SELECT bill_id, MAX(fte) AS fte FROM dw.fact_fiscal_year
                             WHERE table_name = 'all_funds' AND fte IS NOT NULL AND fiscal_year IN (2026, 2027) GROUP BY bill_id""")
    exposure = sql_frame(conn, "SELECT * FROM dw.v_agency_exposure")

    ag_by_bill = agencies.groupby("bill_id").apply(lambda d: list(zip(d.agency_code, d.agency_name))).to_dict()
    md_by_bill = mandates.groupby("bill_id").mandate_type.apply(lambda s: sorted(set(s))).to_dict()
    fte_by_bill = dict(zip(fte.bill_id, fte.fte))
    docs = []
    bills = bills.astype(object).where(bills.notna(), None)   # NaN/NaT → None for text formatting
    for b in bills.itertuples():
        ags = ag_by_bill.get(b.bill_id, [])
        spaced = re.sub(r"^([A-Z]+)(\d+)$", r"\1 \2", b.bill)   # "HB2" and "HB 2": keyword search needs both
        lines = [f"{b.bill} / {spaced} ({b.session_code}): {b.caption}",
                 f"Status: {b.status}. Filed {b.filed_date or 'n/a'}; passed House {b.passed_house_date or '-'}; "
                 f"passed Senate {b.passed_senate_date or '-'}; signed {b.signed_date or '-'}; vetoed {b.vetoed_date or '-'}.",
                 f"House committee: {b.house_committee or '-'} ({b.house_committee_status or '-'}); "
                 f"Senate committee: {b.senate_committee or '-'} ({b.senate_committee_status or '-'}).",
                 f"Authors: {b.authors or '-'}."]
        if b.version:
            lines.append(f"Latest fiscal note: {b.version_label} (version {b.version}); two-year net impact to General "
                         f"Revenue: {money(b.gr_2yr_net)} ({b.gr_2yr_kind}). "
                         f"{'Makes no appropriation. ' if b.no_appropriation else ''}"
                         f"{'Some costs cannot be determined. ' if b.cannot_be_determined else ''}"
                         f"{'New state FTEs: ' + str(fte_by_bill[b.bill_id]) + '.' if b.bill_id in fte_by_bill else ''}")
        else:
            lines.append("No fiscal note published yet.")
        if ags:
            lines.append("Agencies: " + "; ".join(f"{c} {n}" for c, n in ags) + ".")
        if b.bill_id in md_by_bill:
            lines.append("Duties created (AI-extracted): " + ", ".join(md_by_bill[b.bill_id]) + ".")
        docs.append({"id": key("bill", b.bill_id), "doc_type": "bill_card", "session": b.session_code, "bill": b.bill,
                     "title": f"{b.bill} {spaced} ({b.session_code}) {b.caption[:120] if b.caption else ''}",
                     "status": b.status, "agency_codes": [c for c, _ in ags], "content": "\n".join(lines),
                     "source_url": ftp_url(b.note_path) or ftp_url(b.source_path)})
    exposure = exposure.astype(object).where(exposure.notna(), None)
    for e in exposure.itertuples():
        content = (f"{e.agency_name} (agency {e.agency_code}). Enacted bills naming this agency in a fiscal note: "
                   f"{e.enacted_bills}. Two-year (FY2026-27) cost of enacted bills where it is the only agency named: "
                   f"{money(e.direct_cost_2yr)}. AI-attributed two-year cost from fiscal-note text (checked): "
                   f"{money(e.ai_cost_2yr)}; rows needing review: {e.ai_rows_needing_review}. "
                   f"Shared bills (several agencies, cost not split): {e.shared_bills}. New FTEs: {e.new_fte or 0}. "
                   f"FY2024 spending: {money(e.fy2024_spend)}. New cost load as a share of FY2024 spending "
                   f"(per year): {e.load_pct_of_spend if e.load_pct_of_spend is not None else 'n/a'}%.")
        docs.append({"id": key("agency", e.agency_code), "doc_type": "agency_card", "session": "89R", "bill": None,
                     "title": f"Agency {e.agency_code} {e.agency_name}", "status": None, "agency_codes": [e.agency_code],
                     "content": content, "source_url": None})

    text = read_gold(lake, "fiscal_note_text")
    keep = set(bills[(bills.status == "enacted") & bills.gr_2yr_kind.isin(["negative", "positive"])].bill_id) | \
        set(bills[bills.session_code == "90R"].bill_id)
    text["bill_id"] = text.session + ":" + text.bill
    paths = dict(zip(bills.bill_id, bills.note_path))
    for t in text[text.bill_id.isin(keep)].itertuples():
        for section in ("fiscal_analysis", "methodology", "technology", "local_gov"):
            for i, ch in enumerate(chunks(getattr(t, section))):
                docs.append({"id": key("note", t.bill_id, t.version, section, i), "doc_type": "note_section",
                             "session": t.session, "bill": t.bill, "title": f"{t.bill} ({t.session}) fiscal note: {section.replace('_', ' ')}",
                             "status": None, "agency_codes": [c for c, _ in ag_by_bill.get(t.bill_id, [])],
                             "content": ch, "source_url": ftp_url(paths.get(t.bill_id))})
    return docs


def create_index(cli) -> None:
    from azure.search.documents.indexes.models import (SearchableField, SearchFieldDataType, SearchIndex,
                                                       SimpleField)
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="doc_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="session", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="bill", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SimpleField(name="status", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SearchableField(name="agency_codes", collection=True, type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, analyzer_name="en.microsoft"),
        SimpleField(name="source_url", type=SearchFieldDataType.String),
    ]
    try:
        cli.delete_index(INDEX)
    except Exception:  # noqa: BLE001 - first run
        pass
    cli.create_index(SearchIndex(name=INDEX, fields=fields))


def main():
    from azure.identity import DefaultAzureCredential
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient

    from txfr.common import connect_sql, lake_client, load_env

    load_env()
    cred = DefaultAzureCredential()
    docs = build_docs(connect_sql(), lake_client())
    print(f"{len(docs)} documents: " + ", ".join(f"{t}={sum(d['doc_type'] == t for d in docs)}"
                                                for t in ("bill_card", "agency_card", "note_section")))
    create_index(SearchIndexClient(os.environ["SEARCH_ENDPOINT"], cred))
    sc = SearchClient(os.environ["SEARCH_ENDPOINT"], INDEX, cred)
    ok = 0
    for i in range(0, len(docs), 500):
        res = sc.upload_documents(docs[i:i + 500])
        ok += sum(r.succeeded for r in res)
    print(f"uploaded {ok}/{len(docs)} to index {INDEX}")


if __name__ == "__main__":
    main()
