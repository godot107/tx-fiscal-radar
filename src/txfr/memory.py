"""Phase D: per-user memory with the Foundry memory store (preview), called from the agent runner.

    python -m txfr.memory create    # memory store `txfr-analyst` (gpt-4.1-mini extracts, text-embedding-3-small indexes)
    python -m txfr.memory test      # two-session test + isolation check → docs/eval/memory.json
    python -m txfr.memory forget --scope <user>   # delete one user's memories

How it's used (txfr.agent.ask(..., user_scope=...)):
    before the run   recall(scope, question): the user's profile memories plus anything relevant to the question,
                     prepended to the message as "context from earlier conversations"
    after the run    remember(scope, question, answer): the exchange goes to the store, which extracts and
                     consolidates memories in the background (about a minute); the answer never waits for it

Scope = the signed-in user's Entra object id in the web app, so one user's memories never reach another.
The store is told to keep work context only (agency, role, topics, preferred level of detail), not personal data.
The classic agent threads used here (azure-ai-agents 1.1) don't have the memory *tool*; calling the memory store
REST API directly gives the same per-user memory and keeps it visible and testable in this code.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from functools import lru_cache

API = "2025-11-15-preview"
STORE = "txfr-analyst"
PROFILE_RULES = ("Keep only work context useful to a Texas state budget analyst: which agency or office the user works "
                 "for, their role, the bills, agencies or topics they follow, and how much detail they want. Do not keep "
                 "personal data such as age, health, finances, location, contact details or credentials.")


@lru_cache(maxsize=1)
def _cred():
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


def _call(method: str, path: str, body: dict | None = None, ok404: bool = False) -> dict:
    import requests

    from txfr import finops
    finops.add(memory_ops=1)
    tok = _cred().get_token("https://ai.azure.com/.default").token
    url = f"{os.environ['FOUNDRY_PROJECT_ENDPOINT']}/{path}{'&' if '?' in path else '?'}api-version={API}"
    for attempt in range(4):
        r = requests.request(method, url, json=body, timeout=60, headers={"Authorization": f"Bearer {tok}"})
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if ok404 and r.status_code == 404:
            return {}
        if not r.ok:
            raise RuntimeError(f"memory API {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}
    raise RuntimeError(f"memory API {method} {path}: gave up after retries")


def _msg(role: str, text: str) -> dict:
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text[:4000]}]}


def create():
    body = {"name": STORE, "description": "Per-user work context for the tx-session-analyst agent",
            "definition": {"kind": "default", "chat_model": os.environ.get("CHAT_DEPLOYMENT", "gpt-4.1-mini"),
                           "embedding_model": os.environ.get("EMBED_DEPLOYMENT", "text-embedding-3-small"),
                           "options": {"chat_summary_enabled": True, "user_profile_enabled": True,
                                       "procedural_memory_enabled": False, "default_ttl_seconds": 30 * 86400,
                                       "user_profile_details": PROFILE_RULES}}}
    if _call("GET", f"memory_stores/{STORE}", ok404=True):
        print(f"memory store {STORE} exists")
        return
    print(json.dumps(_call("POST", "memory_stores", body), indent=2)[:600])


def recall(scope: str, question: str, k: int = 5) -> list[str]:
    """Profile memories for the user + memories relevant to this question."""
    out = []
    for items in ([], [_msg("user", question)]):
        body = {"scope": scope, "options": {"max_memories": k}, **({"items": items} if items else {})}
        res = _call("POST", f"memory_stores/{STORE}:search_memories", body)
        for m in res.get("memories", []):
            c = (m.get("memory_item") or {}).get("content")
            if c and c not in out:
                out.append(c)
    return out[:k]


def remember(scope: str, question: str, answer: str, wait: bool = False) -> dict:
    """Send one exchange to the store. Extraction runs server-side (~1 min); by default don't wait for it."""
    res = _call("POST", f"memory_stores/{STORE}:update_memories",
                {"scope": scope, "items": [_msg("user", question), _msg("assistant", answer)], "update_delay": 0})
    if wait:
        op = res.get("update_id") or res.get("id")
        for _ in range(40):
            st = _call("GET", f"memory_stores/{STORE}/updates/{op}") if op else {}
            if st.get("status") in ("completed", "succeeded", "failed", "superseded") or not op:
                return st
            time.sleep(5)
    return res


def forget(scope: str):
    print(_call("POST", f"memory_stores/{STORE}:delete_scope", {"scope": scope}))


def test():
    """Session 1 (user A) says where they work; session 2 (new thread, user A) asks about 'my agency';
    user B asks the same question and must NOT get user A's agency."""
    from txfr.agent import ask
    from txfr.common import ROOT, connect_sql
    conn = connect_sql()
    a, b = f"eval-user-a-{int(time.time())}", f"eval-user-b-{int(time.time())}"
    s1 = ask("I work in the budget office at the Department of Information Resources. Which enacted 2025 bills "
             "add new costs or duties for us?", conn, verbose=False, user_scope=a)
    t0, mem_a = time.time(), []
    while time.time() - t0 < 180 and not mem_a:   # extraction is asynchronous (~1 min)
        time.sleep(10)
        mem_a = recall(a, "What new costs is my agency facing?")
    upd = {"status": f"memories visible after {time.time() - t0:.0f}s" if mem_a else "no memories after 180s"}
    s2 = ask("What new costs is my agency facing from the 2025 session?", conn, verbose=False, user_scope=a)
    s3 = ask("What new costs is my agency facing from the 2025 session?", conn, verbose=False, user_scope=b)
    names = ["information resources", "dir"]
    hit = lambda t: any(n in t.lower() for n in names)  # noqa: E731
    result = {"update_status": upd.get("status"), "memories_user_a": mem_a,
              "session2_user_a_mentions_DIR": hit(s2["answer"]), "user_b_mentions_DIR": hit(s3["answer"]),
              "answers": {"s1": s1["answer"][:800], "s2_user_a": s2["answer"][:800], "s2_user_b": s3["answer"][:800]},
              "pass": hit(s2["answer"]) and not hit(s3["answer"])}
    for s in (a, b):
        try:
            forget(s)
        except Exception as e:  # noqa: BLE001
            result.setdefault("cleanup_errors", []).append(str(e)[:200])
    (ROOT / "docs" / "eval" / "memory.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "answers"}, indent=2))


def main():
    from txfr.common import load_env
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["create", "test", "forget"])
    ap.add_argument("--scope")
    a = ap.parse_args()
    if a.cmd == "forget":
        forget(a.scope)
    else:
        {"create": create, "test": test}[a.cmd]()


if __name__ == "__main__":
    main()
