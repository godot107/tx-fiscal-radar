"""Phase 5b: AI extraction from fiscal-note free text (Azure OpenAI gpt-4.1-mini in Foundry, Entra auth).

A fiscal note's tables give one cost for the whole bill; its prose says which agency pays for what
("TEA assumes IT costs of $3.5 million in fiscal year 2026"). The model turns that prose into rows:

    agency_costs  agency_code, fiscal_year, amount (negative = cost, same sign as the note's tables),
                  cost_type, evidence (a verbatim quote)
    mandates      mandate_type (what the bill makes an agency do), agency_code, evidence

Nothing the model says is trusted until it passes deterministic checks (`check_extraction`):
    evidence_grounded   every evidence quote appears verbatim in the note text (no invented sentences)
    agency_listed       every agency_code is one of the note's Source Agencies
    amount_stated       the amount is one of the dollar figures written in the quote (no model arithmetic)
    reconciles          per fiscal year, the agency amounts add up to the note's own total (GR net, all-funds net or
                        all-funds cost lines) within 5% or $50k
A row passing the first three checks is a verified fact (needs_review = 0); failing any → review queue on the
dashboard. `reconciles` says whether a bill's verified rows add up to the note's total (a complete split); when they
don't, the agency figures are a lower bound. Bills that list exactly one agency are a known-answer subset (all cost
belongs to that agency) used to measure completeness (docs/RESULTS.md).

    python -m txfr.ai_extract --session 89R --limit 20     # dry run on 20 enacted bills (prints metrics)
    python -m txfr.ai_extract --session 89R --write        # all enacted bills with a fiscal impact → gold/ai_*
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

PROMPT_VERSION = "v2"   # v2: amount as printed + direction (code applies the sign); amount-in-quote check
COST_TYPES = ["staff", "it", "grants_aid", "contracts", "capital", "other"]
DIRECTIONS = ["cost", "savings", "revenue_gain", "revenue_loss"]
SIGN = {"cost": -1, "revenue_loss": -1, "savings": 1, "revenue_gain": 1}   # the note's own sign convention
MANDATE_TYPES = ["new_program", "reporting", "rulemaking", "licensing_fee", "it_system", "study",
                 "funding_formula", "enforcement", "other"]

SYSTEM = """You extract structured data from Texas Legislative Budget Board fiscal notes.
Rules:
- Use only facts stated in the note text. Never estimate, infer or compute amounts that are not written.
- agency_costs: one row per agency per fiscal year where the text states a dollar amount for that agency.
  amount = the dollar figure exactly as written in the evidence sentence, as a positive number of dollars
  ("$3.5 million" -> 3500000). Do NOT add, subtract or combine figures; if the text gives parts, emit one row per part.
  direction = cost | savings | revenue_gain | revenue_loss.
  agency_code must be one of the provided Source Agencies codes; if the text names an agency not in the list, skip it.
  If the text gives one amount for several years ("$0.3 million in subsequent fiscal years"), emit one row per year
  2026-2030 that the statement covers.
- mandates: what the bill requires an agency to do (new program, reporting, rulemaking, licensing/fee, IT system,
  study, funding formula change, enforcement, other). One row per distinct duty.
- evidence: copy ONE sentence verbatim from the note text that states the fact. Do not paraphrase.
- If the note states no agency-specific amounts, return an empty agency_costs list."""

SCHEMA = {
    "name": "fiscal_note_extraction",
    "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False,
        "required": ["agency_costs", "mandates"],
        "properties": {
            "agency_costs": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["agency_code", "fiscal_year", "amount", "direction", "cost_type", "evidence"],
                "properties": {
                    "agency_code": {"type": "string"}, "fiscal_year": {"type": "integer"},
                    "amount": {"type": "number"}, "direction": {"type": "string", "enum": DIRECTIONS},
                    "cost_type": {"type": "string", "enum": COST_TYPES},
                    "evidence": {"type": "string"}}}},
            "mandates": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["mandate_type", "agency_code", "evidence"],
                "properties": {
                    "mandate_type": {"type": "string", "enum": MANDATE_TYPES},
                    "agency_code": {"type": "string"}, "evidence": {"type": "string"}}}},
        },
    },
}


def _clean(d: dict) -> dict:
    """pandas NaN/NA → None, so text joins and 'is None' checks behave."""
    return {k: (None if v is None or (isinstance(v, float) and v != v) or str(v) in ("<NA>", "NaT") else v) for k, v in d.items()}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def build_prompt(note: dict, agencies: list[dict], years: list[dict]) -> str:
    ag = "\n".join(f"{a['agency_code']} {a['agency_name']}" for a in agencies) or "(none listed)"
    tbl = "\n".join(f"FY{y['fiscal_year']} | {y['line_item']} | {y['amount'] if y.get('amount') is not None else ''}"
                    f"{' | FTE ' + str(y['fte']) if y.get('fte') is not None else ''}" for y in years)
    text = "\n\n".join(f"## {k.replace('_', ' ').title()}\n{note[k]}" for k in
                       ("summary", "fiscal_analysis", "methodology", "technology") if note.get(k))
    return (f"Bill: {note['session']} {note['bill']} (fiscal note version {note['version']})\n\n"
            f"Source Agencies:\n{ag}\n\nNote tables (fiscal year | line item | amount):\n{tbl or '(none)'}\n\n"
            f"Note text:\n{text[:60000]}")


STOP = {"of", "and", "the", "for", "on", "in", "texas", "state", "a", "to"}
GENERIC = {"department", "office", "commission", "board", "agency", "council", "services", "authority", "system",
           "division", "administration", "public", "programs", "within", "trusteed", "health", "human", "general",
           "institute", "university", "school", "college", "center", "fund", "funds", "accounts", "affairs", "education",
           "judicial", "retirement", "resources", "safety", "transportation", "insurance", "workforce"}


def agency_aliases(name: str) -> set[str]:
    """Ways a fiscal note refers to an agency: full name, acronym (Texas Education Agency → TEA, Office of Court
    Administration → OCA, Health and Human Services Commission → HHSC) and distinctive words (Comptroller)."""
    out = {_norm(name)}
    for part in (name or "").split(","):          # "Office of Court Administration, Texas Judicial Council"
        words = [w for w in re.sub(r"[^A-Za-z ]", " ", part).split() if w]
        if not words:
            continue
        out.add(_norm(part))
        ac_all = "".join(w[0] for w in words if w.lower() not in {"of", "and", "the", "for", "on", "in"})
        ac_no_tx = "".join(w[0] for w in words if w.lower() not in STOP)
        for ac in (ac_all, ac_no_tx):
            if len(ac) >= 3:
                out.add(ac.lower())
        out |= {w.lower() for w in words if w.lower() not in STOP | GENERIC and len(w) >= 6}
    return out


def agency_named(evidence: str, name: str) -> bool:
    ev = " " + re.sub(r"[^a-z ]", " ", (evidence or "").lower()) + " "
    return any((f" {a} " in ev) if len(a) <= 5 else (a in ev) for a in agency_aliases(name))


MONEY_IN_TEXT = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|billion|thousand)?", re.I)


def amounts_in(text: str) -> list[float]:
    out = []
    for num, unit in MONEY_IN_TEXT.findall(text or ""):
        v = float(num.replace(",", ""))
        out.append(v * {"million": 1e6, "billion": 1e9, "thousand": 1e3}.get((unit or "").lower(), 1))
    return out


def check_extraction(result: dict, note: dict, agencies: list[dict], years: list[dict]) -> dict:
    """Deterministic checks on one bill's extraction. Returns flags per row and per bill.
        evidence_grounded  the quote appears verbatim in the note text
        amount_stated      the amount is one of the dollar figures written in the quote (catches model arithmetic)
        agency_listed      the agency is one of the note's Source Agencies
        agency_named       on multi-agency bills, the quote names that agency (full name, acronym or keyword)
        reconciles         (bill level) for FY2026 and FY2027 the extracted amounts add up to one of the note's own
                           totals: GR net, all-funds net, or all-funds cost lines, within 5% or $50k"""
    corpus = _norm(" ".join(note.get(k) or "" for k in ("summary", "fiscal_analysis", "methodology", "technology", "local_gov")))
    codes = {a["agency_code"] for a in agencies}
    names = {a["agency_code"]: a.get("agency_name") or "" for a in agencies}
    # The model may cite a line of the note's own tables ("FY2029 | <line item> | -1690086.0"). That is a fact from the
    # note, but a fund-level total, so it can be attributed to an agency only when the bill names exactly one agency.
    table = {(int(y["fiscal_year"]), _norm(str(y["line_item"]))): y.get("amount") for y in years if y.get("amount") is not None}
    rows = []
    for c in result.get("agency_costs", []):
        ev = _norm(c["evidence"]).rstrip(".")
        m = re.match(r"fy(\d{4}) \| (.+?) \| (-?[\d.]+)", ev)
        from_table = bool(m) and table.get((int(m.group(1)), m.group(2).strip())) is not None and \
            abs(abs(table[(int(m.group(1)), m.group(2).strip())]) - abs(c["amount"])) <= 1
        if from_table:
            grounded = stated = len(codes) == 1
        else:
            grounded = len(ev) > 15 and ev in corpus
            stated = any(abs(a - abs(c["amount"])) <= max(0.005 * abs(c["amount"]), 1) for a in amounts_in(c["evidence"]))
        signed = SIGN.get(c.get("direction", "cost"), -1) * abs(c["amount"])
        # Attribution: on a multi-agency bill the quote itself must name the agency (HB 9's Foundation School Program
        # cost was attributed to the Comptroller by v2 without the quote naming it). Single-agency bills: nothing to choose.
        named = len(codes) == 1 or agency_named(c["evidence"], names.get(c["agency_code"], ""))
        rows.append({**c, "signed_amount": signed, "evidence_grounded": grounded, "amount_stated": stated, "from_table": from_table,
                     "agency_named": named,
                     "agency_listed": c["agency_code"] in codes})
    gr, allf, allf_cost = {}, {}, {}
    for y in years:
        if y.get("amount") is None:
            continue
        fy = y["fiscal_year"]
        if y["table_name"] == "gr":
            gr[fy] = gr.get(fy, 0.0) + y["amount"]
        else:
            allf[fy] = allf.get(fy, 0.0) + y["amount"]
            if str(y.get("line_item", "")).startswith("Probable Savings/(Cost)"):
                allf_cost[fy] = allf_cost.get(fy, 0.0) + y["amount"]
    ai_net, ai_cost = {}, {}
    for r in rows:
        fy = r["fiscal_year"]
        ai_net[fy] = ai_net.get(fy, 0.0) + r["signed_amount"]
        if r["direction"] in ("cost", "savings"):
            ai_cost[fy] = ai_cost.get(fy, 0.0) + r["signed_amount"]

    def close(a, b):
        return abs(a - b) <= max(0.05 * abs(b), 50_000)

    fy_ok = {}
    for fy in sorted(set(ai_net) & {2026, 2027}):
        fy_ok[fy] = any(fy in tot and close(ai.get(fy, 0.0), tot[fy])
                        for ai, tot in ((ai_net, gr), (ai_net, allf), (ai_cost, allf_cost), (ai_cost, gr)))
    reconciles = bool(fy_ok) and all(fy_ok.values())
    for r in rows:
        r["reconciles"] = reconciles
        # A row is a verified fact when its quote is in the note, its number is in the quote and its agency is listed.
        # Reconciliation is a bill-level property (is the split complete?), reported separately: notes often itemise
        # only part of a bill's cost in prose, so unreconciled verified rows are true but partial (a lower bound).
        r["needs_review"] = not (r["evidence_grounded"] and r["amount_stated"] and r["agency_listed"] and r["agency_named"])
    mandates = [{**m, "evidence_grounded": _norm(m["evidence"]).rstrip(".") in corpus,
                 "agency_listed": m["agency_code"] in codes} for m in result.get("mandates", [])]
    return {"agency_costs": rows, "mandates": mandates, "reconciles": reconciles, "fy_check": fy_ok,
            "ai_total_2yr": sum(ai_net.get(fy, 0.0) for fy in (2026, 2027)),
            "allfunds_2yr": sum(allf.get(fy, 0.0) for fy in (2026, 2027)) if allf else None,
            "gr_2yr": sum(gr.get(fy, 0.0) for fy in (2026, 2027)) if gr else None}


# ------------------------------------------------------------------ Azure OpenAI client (Entra ID, no keys)
def client():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    tp = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(azure_endpoint=os.environ["OPENAI_ENDPOINT"], azure_ad_token_provider=tp, api_version="2024-10-21")


def extract(cli, prompt: str, cache_dir: Path) -> tuple[dict, dict]:
    """One model call, cached on disk by prompt hash (reruns cost nothing). Returns (result, usage)."""
    key = hashlib.sha256((PROMPT_VERSION + SYSTEM + prompt).encode()).hexdigest()[:24]
    f = cache_dir / f"{key}.json"
    if f.exists():
        d = json.loads(f.read_text())
        return d["result"], {"cached": True, **d.get("usage", {})}
    for attempt in range(5):
        try:
            r = cli.chat.completions.create(
                model=os.environ.get("CHAT_DEPLOYMENT", "gpt-4.1-mini"), temperature=0,
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                response_format={"type": "json_schema", "json_schema": SCHEMA})
            break
        except Exception as e:  # noqa: BLE001 - 429s on a 50K TPM deployment; back off
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))
            last = e  # noqa: F841
    usage = {"prompt_tokens": r.usage.prompt_tokens, "completion_tokens": r.usage.completion_tokens}
    result = json.loads(r.choices[0].message.content)
    cache_dir.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"result": result, "usage": usage}))
    return result, usage


# ------------------------------------------------------------------ lake I/O (gold Parquet)
def read_gold(lake, table: str):
    import pandas as pd

    fs = lake.get_file_system_client("gold")
    parts = [p.name for p in fs.get_paths(path=table) if p.name.endswith(".parquet")]
    frames = [pd.read_parquet(io.BytesIO(fs.get_file_client(p).download_file().readall())) for p in parts]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def write_gold(lake, table: str, df) -> None:
    fs = lake.get_file_system_client("gold")
    for p in list(fs.get_paths(path=table)) if fs.get_directory_client(table).exists() else []:
        if not p.is_directory:
            fs.get_file_client(p.name).delete_file()
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    fs.get_file_client(f"{table}/part-00000.parquet").upload_data(buf.getvalue(), overwrite=True)


def main():
    import pandas as pd

    from txfr.common import ROOT, lake_client, load_env

    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append", help="session code(s); default 89R, 891, 892")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--write", action="store_true", help="write gold/ai_agency_cost + gold/ai_mandate")
    a = ap.parse_args()
    load_env()
    sessions = a.session or ["89R", "891", "892"]
    lake = lake_client()
    bills = read_gold(lake, "bill")
    latest = read_gold(lake, "fiscal_note_latest")
    text = read_gold(lake, "fiscal_note_text")
    agency = read_gold(lake, "fiscal_note_agency")
    years = read_gold(lake, "fiscal_note_year")

    enacted = bills[bills.enacted & bills.session.isin(sessions)][["session", "bill"]]
    work = (latest.merge(enacted, on=["session", "bill"])
            .query("gr_2yr_kind in ['negative', 'positive', 'indeterminate'] or cannot_be_determined")
            .merge(text, on=["session", "bill", "version"]))
    work = work.sort_values(["session", "bill"])
    if a.limit:
        work = work.head(a.limit)
    print(f"{len(work)} enacted bills with a fiscal impact in {sessions}")

    cli = client()
    cache = ROOT / "data" / "ai_cache"
    cost_rows, mandate_rows, bill_rows, checks = [], [], [], []
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "cached": 0}
    for _, n in work.iterrows():
        key = {"session": n.session, "bill": n.bill}
        ag = [_clean(r) for r in agency.merge(pd.DataFrame([key]), on=["session", "bill"]).to_dict("records")]
        yr = [_clean(r) for r in years.merge(pd.DataFrame([key]), on=["session", "bill"]).to_dict("records")]
        note = _clean(n.to_dict())
        result, usage = extract(cli, build_prompt(note, ag, yr), cache)
        tokens["calls"] += 1
        tokens["cached"] += int(usage.get("cached", False))
        if not usage.get("cached"):
            tokens["prompt_tokens"] += usage["prompt_tokens"]
            tokens["completion_tokens"] += usage["completion_tokens"]
        chk = check_extraction(result, note, ag, yr)
        bill_rows.append({**key, "n_agencies": len(ag), "n_cost_rows": len(chk["agency_costs"]),
                          "reconciles": chk["reconciles"], "ai_total_2yr": chk["ai_total_2yr"],
                          "allfunds_2yr": chk["allfunds_2yr"], "gr_2yr": chk["gr_2yr"],
                          "grounded_rate": (sum(r["evidence_grounded"] for r in chk["agency_costs"]) / len(chk["agency_costs"]))
                          if chk["agency_costs"] else None})
        checks += [{"evidence_grounded": r["evidence_grounded"], "agency_listed": r["agency_listed"],
                    "amount_stated": r["amount_stated"], "agency_named": r["agency_named"]} for r in chk["agency_costs"]]
        for r in chk["agency_costs"]:
            cost_rows.append({**key, "version": n.version, "agency_code": r["agency_code"], "fiscal_year": int(r["fiscal_year"]),
                              "amount": float(r["signed_amount"]),
                              "cost_type": "revenue" if r["direction"].startswith("revenue") else r["cost_type"],
                              "evidence": r["evidence"],
                              "reconciles": bool(r["reconciles"]), "needs_review": bool(r["needs_review"]),
                              "model": f"{os.environ.get('CHAT_DEPLOYMENT', 'gpt-4.1-mini')}/{PROMPT_VERSION}"})
        for m in chk["mandates"]:
            mandate_rows.append({**key, "mandate_type": m["mandate_type"],
                                 "agency_code": m["agency_code"] if m["agency_listed"] else None,
                                 "evidence": m["evidence"],
                                 "model": f"{os.environ.get('CHAT_DEPLOYMENT', 'gpt-4.1-mini')}/{PROMPT_VERSION}"})
    b = pd.DataFrame(bill_rows)
    c = pd.DataFrame(cost_rows)
    metrics = summarize(b, c, pd.DataFrame(checks), tokens)
    print(json.dumps(metrics, indent=2))
    out = ROOT / "data" / "ai_metrics.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2))
    b.to_csv(ROOT / "data" / "ai_bills.csv", index=False)
    if a.write:
        write_gold(lake, "ai_agency_cost", c if len(c) else pd.DataFrame(columns=["session", "bill"]))
        write_gold(lake, "ai_mandate", pd.DataFrame(mandate_rows))
        print("wrote gold/ai_agency_cost, gold/ai_mandate")


def summarize(b, c, k, tokens) -> dict:
    with_costs = b[b.n_cost_rows > 0]
    single = with_costs[with_costs.n_agencies == 1]
    price_in, price_out = 0.40, 1.60   # gpt-4.1-mini $ per 1M tokens (GlobalStandard list price)
    return {
        "bills": int(len(b)),
        "bills_with_agency_amounts": int(len(with_costs)),
        "cost_rows": int(len(c)),
        "evidence_grounded_rate": round(float(k.evidence_grounded.mean()), 3) if len(k) else None,
        "agency_listed_rate": round(float(k.agency_listed.mean()), 3) if len(k) else None,
        "amount_stated_rate": round(float(k.amount_stated.mean()), 3) if len(k) else None,
        "agency_named_rate": round(float(k.agency_named.mean()), 3) if len(k) else None,
        "reconcile_rate_bills_with_amounts": round(float(with_costs.reconciles.mean()), 3) if len(with_costs) else None,
        "known_answer_single_agency_bills": int(len(single)),
        "known_answer_reconcile_rate": round(float(single.reconciles.mean()), 3) if len(single) else None,
        "rows_needing_review": int(c.needs_review.sum()) if len(c) else 0,
        "verified_rows": int((~c.needs_review).sum()) if len(c) else 0,
        "tokens": tokens,
        "est_cost_usd": round(tokens["prompt_tokens"] / 1e6 * price_in + tokens["completion_tokens"] / 1e6 * price_out, 3),
    }


if __name__ == "__main__":
    main()
