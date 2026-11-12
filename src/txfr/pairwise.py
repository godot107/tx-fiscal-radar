"""A/B evaluation: which of two agent variants gives the more useful answer? (Offline, LLM judge.)

    python -m txfr.pairwise --a guarded --b knowledge      # docs/eval/<a>_runs.jsonl vs <b>_runs.jsonl → docs/eval/pairwise_<a>_vs_<b>.json

Same questions, two saved answers each (no agent calls). A gpt-4.1-mini judge picks A, B or tie on usefulness to a
budget analyst: correct, specific figures with sources, direct, honest about limits. Every pair is judged twice with
the order swapped; a variant wins only if it wins both orders (otherwise tie). That cancels position bias, the
judge's known habit of preferring whichever answer it reads first.

Report: wins / losses / ties, win rate among decided pairs with a 95% Wilson interval. With ~26 questions the
interval is wide: this tells you about large differences only. Online votes from the web app (PREF_EVENT, see
docs/EVALUATION.md) are the same comparison with people as the judge.
"""
from __future__ import annotations

import argparse
import json
import math
import os

from txfr.common import ROOT

EVAL = ROOT / "docs" / "eval"
SCHEMA = {"name": "preference", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["winner", "reason"],
    "properties": {"winner": {"type": "string", "enum": ["first", "second", "tie"]}, "reason": {"type": "string"}}}}
PROMPT = ("You compare two answers from an assistant for Texas state budget analysts. Pick the more useful answer: "
          "correct and specific figures, sources cited, answers the question directly, honest about what the data "
          "can't say, neutral. Length is not quality. If they are equally good or equally bad, say tie. "
          "reason: one sentence.")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


def _judge():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI
    cli = AzureOpenAI(azure_endpoint=os.environ["OPENAI_ENDPOINT"], api_version="2024-10-21",
                      azure_ad_token_provider=get_bearer_token_provider(DefaultAzureCredential(),
                                                                        "https://cognitiveservices.azure.com/.default"))

    def pick(q, first, second):
        r = cli.chat.completions.create(model=os.environ.get("CHAT_DEPLOYMENT", "gpt-4.1-mini"), temperature=0,
                                        response_format={"type": "json_schema", "json_schema": SCHEMA},
                                        messages=[{"role": "system", "content": PROMPT},
                                                  {"role": "user", "content": f"Question: {q}\n\nFIRST answer:\n{first[:4000]}"
                                                                              f"\n\nSECOND answer:\n{second[:4000]}"}])
        return json.loads(r.choices[0].message.content)
    return pick


def compare(a: str, b: str):
    load = lambda lbl: {json.loads(l)["case"]["id"]: json.loads(l) for l in (EVAL / f"{lbl}_runs.jsonl").read_text().splitlines()}  # noqa: E731
    ra, rb = load(a), load(b)
    pick, rows = _judge(), []
    for cid in sorted(set(ra) & set(rb)):
        q, xa, xb = ra[cid]["question"], ra[cid]["answer"], rb[cid]["answer"]
        j1, j2 = pick(q, xa, xb), pick(q, xb, xa)          # A first, then B first
        v1 = {"first": "a", "second": "b", "tie": "tie"}[j1["winner"]]
        v2 = {"first": "b", "second": "a", "tie": "tie"}[j2["winner"]]
        winner = v1 if v1 == v2 else "tie"                # must win in both orders
        rows.append({"id": cid, "question": q, "winner": winner, "order_1": v1, "order_2": v2,
                     "reason_1": j1["reason"], "reason_2": j2["reason"], "check_a": ra[cid].get("check"),
                     "check_b": rb[cid].get("check")})
        print(f"{cid} {winner:4}  ({v1}/{v2})  {q[:70]}")
    wins_a, wins_b = sum(r["winner"] == "a" for r in rows), sum(r["winner"] == "b" for r in rows)
    decided = wins_a + wins_b
    summary = {"a": a, "b": b, "pairs": len(rows), "a_wins": wins_a, "b_wins": wins_b, "ties": len(rows) - decided,
               "a_win_rate_decided": round(wins_a / decided, 3) if decided else None,
               "a_win_rate_ci95": wilson(wins_a, decided),
               "position_flips": sum(r["order_1"] != r["order_2"] for r in rows),
               "note": "a variant wins a pair only if it wins with both answer orders"}
    out = EVAL / f"pairwise_{a}_vs_{b}.json"
    out.write_text(json.dumps({"summary": summary, "pairs": rows}, indent=2))
    print(json.dumps(summary, indent=2))


def main():
    from txfr.common import load_env
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    x = ap.parse_args()
    compare(x.a, x.b)


if __name__ == "__main__":
    main()
