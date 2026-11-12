"""Phase G: how far does Fiscal Note Desk scale on this subscription? Ramp concurrent users and measure.

    .venv/bin/python tools/loadtest.py --label tpm50              # stages 1, 5, 10, 20 concurrent users
    .venv/bin/python tools/loadtest.py --label tpm300 --stages 20  # after raising the model deployment's capacity
    .venv/bin/python tools/loadtest.py --chart tpm50 tpm300        # docs/img/loadtest_*.png

Each simulated user asks questions back to back (no think time) for a fixed number of requests per stage, through
the real path: Entra token → App Service Authentication → Function App → Foundry agent → tools → guardrails.
Memory is off for the test (?memory=0) so test questions don't become anyone's memories.

Question mix: 8 questions, 6 answered from SQL metrics and 2 that search fiscal notes (the semantic ranker's free
tier allows 1,000 queries a month; the agent falls back to vector search past that).
Recorded per request: HTTP status, latency, prompt/completion tokens, error text. Per stage: p50/p95 latency,
answers per minute, throttled share (HTTP 429), error share, tokens per minute.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT / "docs" / "eval"
QUESTIONS = [
    "Which state agency carries the largest new two-year cost from enacted 2025 bills?",
    "How many House and Senate bills were enacted in the 2025 regular session?",
    "What is the status of HB 2 from the 2025 regular session and its two-year cost?",
    "Which agencies face the largest new cost relative to their FY2024 spending?",
    "How many bills have been filed so far for the 2027 regular session?",
    "Which enacted 2025 bills have costs that grow after 2026-27?",
    "Did the Legislature pass anything on school vouchers in 2025?",
    "Why does SB 2206 cost nothing in 2026-27 but more later?",
]


def token(app_id: str) -> str:
    return subprocess.run(["az", "account", "get-access-token", "--resource", f"api://{app_id}", "--query", "accessToken",
                           "-o", "tsv"], capture_output=True, text=True, check=True).stdout.strip()


def one(url: str, tok: str, q: str) -> dict:
    import requests
    t = time.time()
    try:
        r = requests.post(f"{url}/api/ask?memory=0", json={"question": q + _salt()}, timeout=180,
                          headers={"Authorization": f"Bearer {tok}"})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {"status": r.status_code, "seconds": round(time.time() - t, 2), "q": q,
                "tokens": body.get("tokens"), "error": (body.get("error") or body.get("detail") or
                                                        (r.text[:200] if r.status_code >= 400 else None)),
                "server_seconds": body.get("seconds"), "run_status": body.get("status")}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "seconds": round(time.time() - t, 2), "q": q, "error": f"{type(e).__name__}: {e}"[:200]}


_salt_on = True


def _salt() -> str:
    """A short reference suffix so the answer cache (keyed by normalized text) doesn't serve load-test repeats:
    the test measures the agent path. --cached turns this off to measure the cache path."""
    import random
    return f" (ref {random.randint(1000, 9999)})" if _salt_on else ""


def flood(url: str, n: int, workers: int = 50) -> dict:
    """Unauthenticated burst: what does an anonymous flood cost? (Expect 401s from App Service Authentication.)"""
    import requests
    t0, codes, lat = time.time(), {}, []

    def hit(_):
        t = time.time()
        try:
            c = requests.post(f"{url}/api/ask", json={"question": "x"}, timeout=30).status_code
        except Exception:  # noqa: BLE001
            c = 0
        lat.append(time.time() - t)
        return c
    with cf.ThreadPoolExecutor(workers) as ex:
        for c in ex.map(hit, range(n)):
            codes[c] = codes.get(c, 0) + 1
    lat.sort()
    out = {"requests": n, "workers": workers, "wall_seconds": round(time.time() - t0, 1), "status_codes": codes,
           "p50_ms": round(lat[len(lat) // 2] * 1000), "p95_ms": round(lat[int(0.95 * len(lat))] * 1000)}
    (OUT / "loadtest_flood.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out))
    return out


def _throttled(r: dict) -> bool:
    """Model rate limiting only (the app's own per-user cost cap is counted separately)."""
    e = str(r.get("error")).lower()
    return "daily limit" not in e and (r["status"] == 429 or "rate_limit" in e or "rate limit" in e)


def stage(url: str, tok: str, users: int, per_user: int) -> dict:
    results, lock, t0 = [], threading.Lock(), time.time()

    def user(u):
        for i in range(per_user):
            r = one(url, tok, QUESTIONS[(u * 3 + i) % len(QUESTIONS)])
            with lock:
                results.append(r)
    with cf.ThreadPoolExecutor(users) as ex:
        list(ex.map(user, range(users)))
    wall = time.time() - t0
    ok = [r for r in results if r["status"] == 200 and not r.get("error")]
    lat = sorted(r["seconds"] for r in ok)
    pct = lambda p: lat[min(int(p * len(lat)), len(lat) - 1)] if lat else None  # noqa: E731
    toks = sum((r.get("tokens") or {}).get("prompt") or 0 for r in ok) + sum((r.get("tokens") or {}).get("completion") or 0 for r in ok)
    return {"users": users, "requests": len(results), "ok": len(ok), "wall_seconds": round(wall, 1),
            "answers_per_min": round(len(ok) / wall * 60, 1), "p50_s": pct(0.5), "p95_s": pct(0.95),
            "throttled_share": round(sum(_throttled(r) for r in results) / len(results), 3),
            "cost_capped_share": round(sum("daily limit" in str(r.get("error")).lower() for r in results) / len(results), 3),
            "error_share": round(sum(r["status"] != 200 or bool(r.get("error")) for r in results) / len(results), 3),
            "tokens_per_min": round(toks / wall * 60), "tokens_per_answer": round(toks / len(ok)) if ok else None,
            "errors": sorted({str(r.get("error"))[:120] for r in results if r.get("error")})[:6], "raw": results}


def run(label: str, stages: list[int], per_user: int):
    from txfr.common import load_env
    load_env()
    url, tok = os.environ["APP_URL"], token(os.environ["APP_CLIENT_ID"])
    print("warm-up:", one(url, tok, QUESTIONS[0])["status"])   # cold start + SQL resume aren't part of the measurement
    out = {"label": label, "per_user": per_user, "stages": []}
    for u in stages:
        s = stage(url, tok, u, per_user)
        out["stages"].append(s)
        print(json.dumps({k: v for k, v in s.items() if k != "raw"}))
        (OUT / f"loadtest_{label}.json").write_text(json.dumps(out, indent=2, default=str))


NAMES = {"tpm50": "50K tokens/min", "tpm300": "300K, no output cap", "tpm300_capped": "300K, 2,000-token output cap"}


def chart(labels: list[str]):
    import matplotlib.pyplot as plt
    runs = {l: json.loads((OUT / f"loadtest_{l}.json").read_text()) for l in labels}
    for d in runs.values():   # recompute from raw results so old runs use the same definitions
        for st in d["stages"]:
            st["throttled_share"] = round(sum(_throttled(r) for r in st["raw"]) / len(st["raw"]), 3)
    colors = ["#2563eb", "#c2410c", "#15803d"]
    for metric, ylab, fname in (("answers_per_min", "Answers per minute", "loadtest_throughput.png"),
                                ("p95_s", "95th-percentile latency (s)", "loadtest_latency.png"),
                                ("throttled_share", "Requests throttled by the model quota", "loadtest_throttled.png")):
        fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=150)
        for (l, d), c in zip(runs.items(), colors, strict=False):
            xs = [s["users"] for s in d["stages"]]
            ys = [s[metric] if s[metric] is not None else float("nan") for s in d["stages"]]
            ax.plot(xs, ys, marker="o", lw=2, ms=6, color=c, label=NAMES.get(l, l))
        ax.set_xticks([1, 5, 10, 20])
        ax.set_xlabel("Concurrent users")
        ax.set_ylabel(ylab)
        ax.grid(axis="y", color="#e5e5e5", lw=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        if metric == "throttled_share":
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
        ax.legend(frameon=False, fontsize=8.5, loc="upper left")
        fig.tight_layout()
        fig.savefig(ROOT / "docs" / "img" / fname)
        plt.close(fig)
        print("wrote", fname)
    for l, d in runs.items():
        (OUT / f"loadtest_{l}.json").write_text(json.dumps(d, indent=2, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="tpm50")
    ap.add_argument("--stages", type=int, nargs="+", default=[1, 5, 10, 20])
    ap.add_argument("--per-user", type=int, default=6)
    ap.add_argument("--chart", nargs="+")
    ap.add_argument("--flood", type=int, help="send N unauthenticated requests instead")
    ap.add_argument("--cached", action="store_true", help="identical questions: measure the answer-cache path")
    a = ap.parse_args()
    global _salt_on
    _salt_on = not a.cached
    if a.flood:
        from txfr.common import load_env
        load_env()
        flood(os.environ["APP_URL"], a.flood)
    else:
        chart(a.chart) if a.chart else run(a.label, a.stages, a.per_user)


if __name__ == "__main__":
    main()
