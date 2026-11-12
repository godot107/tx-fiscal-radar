"""Phase E: fine-tune gpt-4.1-mini on its own verified extractions, and test whether it beats the prompted model.

    python -m txfr.finetune prep      # train / validation / holdout JSONL from the cached extractions (no model calls)
    python -m txfr.finetune train     # upload files, start a Developer-tier fine-tuning job
    python -m txfr.finetune status    # job status; when it succeeds, prints the model id
    python -m txfr.finetune deploy    # Developer-tier deployment `gpt-4.1-mini-ft` (no hourly fee; auto-deleted after 24 h)
    python -m txfr.finetune compare   # holdout bills: prompted (cached) vs fine-tuned, same four checks → docs/eval/finetune.json

What this is: distillation with a verifier. The training targets are the prompted model's own outputs, filtered to
the rows that passed all four deterministic checks (quote in the note, number in the quote, agency listed, agency
named). The question is whether training on only the verified rows teaches the model to produce more verifiable
rows on bills it hasn't seen. It is not "teaching it new facts".

Split by bill (seeded), so no bill's text appears in both training and holdout. Bills whose verified output is
empty are kept as examples of "nothing checkable here" only when the note has no agency table lines.
Cost: training ≈ 2 epochs x ~0.5M tokens at the Developer training rate; inference on the holdout ≈ 60 calls.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time

from txfr.common import ROOT

OUT = ROOT / "data" / "finetune"
EVAL = ROOT / "docs" / "eval" / "finetune.json"
FT_DEPLOYMENT = "gpt-4.1-mini-ft"
SEED, HOLDOUT_FRAC, VAL_FRAC = 7, 0.3, 0.1


def _work():
    """Same selection as txfr.ai_extract.main: enacted bills with a fiscal impact, with their prompt inputs."""
    import pandas as pd

    from txfr.ai_extract import _clean, read_gold
    from txfr.common import lake_client
    lake = lake_client()
    bills, latest = read_gold(lake, "bill"), read_gold(lake, "fiscal_note_latest")
    text, agency, years = read_gold(lake, "fiscal_note_text"), read_gold(lake, "fiscal_note_agency"), read_gold(lake, "fiscal_note_year")
    enacted = bills[bills.enacted & bills.session.isin(["89R", "891", "892"])][["session", "bill"]]
    work = (latest.merge(enacted, on=["session", "bill"])
            .query("gr_2yr_kind in ['negative', 'positive', 'indeterminate'] or cannot_be_determined")
            .merge(text, on=["session", "bill", "version"]).sort_values(["session", "bill"]))
    for _, n in work.iterrows():
        key = {"session": n.session, "bill": n.bill}
        ag = [_clean(r) for r in agency.merge(pd.DataFrame([key]), on=["session", "bill"]).to_dict("records")]
        yr = [_clean(r) for r in years.merge(pd.DataFrame([key]), on=["session", "bill"]).to_dict("records")]
        yield key, _clean(n.to_dict()), ag, yr


def _verified_only(result: dict, chk: dict) -> dict:
    """The model's output, keeping only cost rows that passed all four checks and mandates grounded in the note."""
    costs = [{k: r[k] for k in ("agency_code", "fiscal_year", "amount", "direction", "cost_type", "evidence")}
             for r in chk["agency_costs"] if not r["needs_review"]]
    mandates = [{k: m[k] for k in ("mandate_type", "agency_code", "evidence")}
                for m in chk["mandates"] if m["evidence_grounded"] and m["agency_listed"]]
    return {**result, "agency_costs": costs, "mandates": mandates}


def prep():
    from txfr.ai_extract import SYSTEM, build_prompt, check_extraction
    OUT.mkdir(parents=True, exist_ok=True)
    cache = ROOT / "data" / "ai_cache"
    items = []
    for key, note, ag, yr in _work():
        prompt = build_prompt(note, ag, yr)
        result, _ = _cached(prompt, cache)   # prompted model's output from the extraction run (no new calls)
        if result is None:
            continue
        chk = check_extraction(result, note, ag, yr)
        target = _verified_only(result, chk)
        items.append({"key": key, "prompt": prompt, "target": target,
                      "n_rows": len(result.get("agency_costs", [])), "n_verified": len(target["agency_costs"])})
    rng = random.Random(SEED)
    rng.shuffle(items)
    n_hold = int(len(items) * HOLDOUT_FRAC)
    hold, rest = items[:n_hold], items[n_hold:]
    # train only on bills where something was verified, or where the model found nothing at all
    rest = [i for i in rest if i["n_verified"] > 0 or i["n_rows"] == 0]
    n_val = max(int(len(rest) * VAL_FRAC), 5)
    val, train = rest[:n_val], rest[n_val:]

    def chat(i):
        return {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": i["prompt"]},
                             {"role": "assistant", "content": json.dumps(i["target"])}]}
    for name, rows in (("train", train), ("validation", val)):
        (OUT / f"{name}.jsonl").write_text("".join(json.dumps(chat(i)) + "\n" for i in rows))
    (OUT / "holdout.json").write_text(json.dumps([i["key"] for i in hold]))
    toks = sum(len(json.dumps(chat(i))) for i in train) / 4
    print(f"{len(items)} bills: train {len(train)}, validation {len(val)}, holdout {len(hold)} "
          f"(dropped {int(len(items) * (1 - HOLDOUT_FRAC)) - len(train) - len(val)} with nothing verified); "
          f"~{toks / 1e6:.2f}M training tokens per epoch")


def _cached(prompt: str, cache):
    import hashlib

    from txfr.ai_extract import PROMPT_VERSION, SYSTEM
    f = cache / f"{hashlib.sha256((PROMPT_VERSION + SYSTEM + prompt).encode()).hexdigest()[:24]}.json"
    return (json.loads(f.read_text())["result"], {"cached": True}) if f.exists() else (None, None)


def _aoai():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    tp = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(azure_endpoint=os.environ["OPENAI_ENDPOINT"], azure_ad_token_provider=tp,
                       api_version="2025-04-01-preview")


def train(epochs: int = 2):
    cli = _aoai()
    ids = {}
    for name in ("train", "validation"):
        f = cli.files.create(file=open(OUT / f"{name}.jsonl", "rb"), purpose="fine-tune")
        ids[name] = f.id
    for fid in ids.values():   # files must finish processing before a job can use them
        while cli.files.retrieve(fid).status not in ("processed", "error"):
            time.sleep(10)
    job = cli.fine_tuning.jobs.create(model="gpt-4.1-mini-2025-04-14", training_file=ids["train"],
                                      validation_file=ids["validation"], suffix="txfr",
                                      method={"type": "supervised", "supervised": {"hyperparameters": {"n_epochs": epochs}}},
                                      seed=SEED, extra_body={"trainingType": "developerTier"})
    (OUT / "job.json").write_text(json.dumps({"job_id": job.id, "files": ids}))
    print(f"job {job.id}: {job.status}")


def status():
    cli = _aoai()
    job = cli.fine_tuning.jobs.retrieve(json.loads((OUT / "job.json").read_text())["job_id"])
    print(job.status, job.fine_tuned_model or "", job.trained_tokens or "", job.error or "")
    for e in list(cli.fine_tuning.jobs.list_events(job.id, limit=5)):
        print("  ", e.created_at, e.message[:150])
    return job


def deploy():
    import subprocess
    job = status()
    if not job.fine_tuned_model:
        raise SystemExit("job not finished")
    acct = os.environ["OPENAI_ENDPOINT"].split("//")[1].split(".")[0]
    rg = os.environ.get("RG", "rg-tx-fiscal-radar-dev")
    sub = subprocess.run(["az", "account", "show", "--query", "id", "-o", "tsv"], capture_output=True, text=True).stdout.strip()
    url = (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/"
           f"accounts/{acct}/deployments/{FT_DEPLOYMENT}?api-version=2025-06-01")
    body = {"sku": {"name": "DeveloperTier", "capacity": 50},
            "properties": {"model": {"format": "OpenAI", "name": job.fine_tuned_model, "version": "1"}}}
    r = subprocess.run(["az", "rest", "--method", "put", "--url", url, "--body", json.dumps(body)], capture_output=True, text=True)
    print(r.stdout[-400:] or r.stderr[-800:])


def compare():
    from txfr.ai_extract import SCHEMA, SYSTEM, build_prompt, check_extraction
    hold = {(k["session"], k["bill"]) for k in json.loads((OUT / "holdout.json").read_text())}
    cli, cache = _aoai(), ROOT / "data" / "ai_cache"
    rows, toks = [], {"prompt": 0, "completion": 0}
    for key, note, ag, yr in _work():
        if (key["session"], key["bill"]) not in hold:
            continue
        prompt = build_prompt(note, ag, yr)
        base, _ = _cached(prompt, cache)
        r = None
        for attempt in range(5):
            try:
                r = cli.chat.completions.create(model=FT_DEPLOYMENT, temperature=0,
                                                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                                response_format={"type": "json_schema", "json_schema": SCHEMA})
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 4:
                    raise
                print("retry", str(e)[:120])
                time.sleep(5 * (attempt + 1))
        toks["prompt"] += r.usage.prompt_tokens
        toks["completion"] += r.usage.completion_tokens
        ft = json.loads(r.choices[0].message.content)
        row = {**key}
        for name, res in (("prompted", base), ("finetuned", ft)):
            if res is None:
                row[name] = None
                continue
            chk = check_extraction(res, note, ag, yr)
            n = len(chk["agency_costs"])
            row[name] = {"rows": n, "verified": sum(not x["needs_review"] for x in chk["agency_costs"]),
                         "reconciles": bool(chk["reconciles"])}
        rows.append(row)
        print(key["session"], key["bill"], row["prompted"], row["finetuned"])

    def agg(name):
        sel = [r[name] for r in rows if r.get(name)]
        n, v = sum(x["rows"] for x in sel), sum(x["verified"] for x in sel)
        return {"bills": len(sel), "rows": n, "verified_rows": v, "verified_rate": round(v / n, 3) if n else None,
                "bills_reconciling": sum(x["reconciles"] for x in sel)}
    summary = {"holdout_bills": len(rows), "prompted": agg("prompted"), "finetuned": agg("finetuned"),
               "finetuned_inference_tokens": toks}
    EVAL.write_text(json.dumps({"summary": summary, "bills": rows}, indent=2))
    print(json.dumps(summary, indent=2))


def main():
    from txfr.common import load_env
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "train", "status", "deploy", "compare"])
    ap.add_argument("--epochs", type=int, default=2)
    a = ap.parse_args()
    {"prep": prep, "train": lambda: train(a.epochs), "status": status, "deploy": deploy, "compare": compare}[a.cmd]()


if __name__ == "__main__":
    main()
