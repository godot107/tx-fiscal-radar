"""Phase F: Fiscal Note Desk, the web front end for the session-analyst agent.

One Azure Functions app (Flex Consumption, Python 3.12) serves both:
    GET  /page      the page (static/index.html); an empty route maps to the function name
    POST /api/ask   {"question": "..."} → answer, tool calls, guardrail flags, memories used, timing
    GET  /api/health
Sign-in: App Service Authentication (Entra ID) in front of every route; unauthenticated requests get 401/redirect
before any code here runs. The signed-in user's object id (header x-ms-client-principal-id, set by the platform)
is the memory scope, so each user gets their own memory.
Identity to Azure: the app's system-assigned managed identity (Foundry agent, Content Safety, AI Search, Azure SQL).
No keys or connection strings anywhere.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)   # Entra auth is enforced by the platform
_local = threading.local()
DAILY_CAP_USD = float(os.environ.get("TXFR_USER_DAILY_USD", "1.0"))   # FinOps: per-user daily spend cap
SBS_SHARE = float(os.environ.get("TXFR_SBS_SHARE", "0"))   # A/B: share of questions answered side by side
VARIANTS = {"client_hybrid": "AGENT_ID", "foundry_knowledge": "AGENT_ID_KNOWLEDGE"}   # name → app setting
_spend_lock = threading.Lock()
CACHE_ON = os.environ.get("TXFR_CACHE", "1") == "1"
from txfr.cache_policy import cacheable  # noqa: E402 - which answers may be cached (tested in tests/)
LOG_SALT = os.environ.get("TXFR_LOG_SALT", "")


def pid(uid: str | None) -> str | None:
    """Pseudonymous user id for logs, counters and analytics: a keyed hash of the Entra object id. Per-person counts
    still work; nobody reading the logs can see who asked. (Memory, which is opt-in, keeps the real id as its scope.)"""
    import hashlib
    return None if not uid else hashlib.sha256(f"{LOG_SALT}:{uid}".encode()).hexdigest()[:16]
PAGE = (Path(__file__).parent / "static" / "index.html").read_text()
MAX_Q = 500


def _conn():
    """One SQL connection per worker thread (connections aren't thread-safe); reopened if it dropped."""
    from txfr.common import connect_sql
    c = getattr(_local, "conn", None)
    if c is not None:
        try:   # liveness check: the free-offer database pauses after 60 idle minutes and drops connections
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.close()
        except Exception:  # noqa: BLE001
            c = None
    if c is None:
        c = _local.conn = connect_sql(retries=4, wait=15)
    return c


def guard_event(kind: str, user: str | None, **detail) -> None:
    """Blue team (detect): one structured line per guardrail trigger → App Insights AppTraces. The alert rule in
    infra/main.bicep counts these; docs/SECURITY.md has the KQL. No question text or answer is logged, only the
    category, the user's object id and counts."""
    logging.warning("GUARD_EVENT %s", json.dumps({"kind": kind, "user": pid(user), **detail}, default=str))


def _guard_events(r: dict, uid: str | None, route: str) -> None:
    g = r.get("guard") or {}
    sc = g.get("screen") or {}
    if r.get("status") == "refused_by_policy":
        guard_event("policy_block", uid, route=route, category=sc.get("category"), by=sc.get("blocked_by", "screen"))
    if g.get("shield_error"):
        guard_event("shield_error", uid, route=route)
    elif g.get("docs_removed"):
        guard_event("injection_removed", uid, route=route, docs=g["docs_removed"])
    gr = g.get("groundedness") or {}
    if gr.get("ungrounded") and (gr.get("ungrounded_pct") or 0) >= 0.3:
        guard_event("ungrounded_answer", uid, route=route, pct=gr.get("ungrounded_pct"))
    err = str(r.get("error") or "")
    if "content_filter" in err or "ResponsibleAI" in err or "jailbreak" in err.lower():
        guard_event("content_filter", uid, route=route)


def _table():
    """Shared per-user daily spend (Table Storage, managed identity): the cap holds across all instances."""
    t = getattr(_local, "table", None)
    if t is None:
        from azure.data.tables import TableClient
        from azure.identity import DefaultAzureCredential
        t = _local.table = TableClient(f"https://{os.environ['APP_STORAGE_ACCOUNT']}.table.core.windows.net",
                                       "spend", credential=DefaultAzureCredential())
    return t


def _spend_key(uid):
    return time.strftime("%Y-%m-%d", time.gmtime()), pid(uid) or "anonymous"


def _spent_today(uid: str | None) -> float:
    pk, rk = _spend_key(uid)
    try:
        return float(_table().get_entity(pk, rk).get("usd", 0.0))
    except Exception:  # noqa: BLE001 - not found, or storage hiccup: don't block the user on the counter
        return 0.0


def _add_spend(uid: str | None, usd: float) -> None:
    from azure.core import MatchConditions
    pk, rk = _spend_key(uid)
    for _ in range(3):   # optimistic concurrency: two instances updating the same user retry instead of losing a write
        try:
            e = _table().get_entity(pk, rk)
            e["usd"] = float(e.get("usd", 0.0)) + usd
            _table().update_entity(e, etag=e.metadata["etag"], match_condition=MatchConditions.IfNotModified)
            return
        except Exception as ex:  # noqa: BLE001
            if "ResourceNotFound" in type(ex).__name__ + str(ex):
                try:
                    _table().create_entity({"PartitionKey": pk, "RowKey": rk, "usd": usd})
                    return
                except Exception:  # noqa: BLE001 - created by someone else meanwhile: retry the update
                    continue
            continue
    logging.warning("spend counter update failed for %s", rk)


# ------------------------------------------------------------------ answer cache (data changes once a night)
def _cache_key(q: str) -> str | None:
    """Normalized question + the date of the last warehouse load: tonight's load invalidates the whole cache."""
    import hashlib
    import re as _re
    try:
        cur = _conn().cursor()
        cur.execute("SELECT CONVERT(varchar(19), MAX(last_load_utc), 126) FROM dw.v_data_quality")
        loaded = cur.fetchall()[0][0]
        cur.close()
    except Exception:  # noqa: BLE001
        return None
    norm = _re.sub(r"[^a-z0-9 ]", "", " ".join(q.lower().split()))
    return f"{loaded}/{hashlib.sha256(norm.encode()).hexdigest()[:32]}.json"


def _blob(name: str):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobClient
    return BlobClient(f"https://{os.environ['APP_STORAGE_ACCOUNT']}.blob.core.windows.net", "answercache", name,
                      credential=DefaultAzureCredential())


def _cache_get(key: str | None) -> dict | None:
    if not (CACHE_ON and key):
        return None
    try:
        return json.loads(_blob(key).download_blob().readall())
    except Exception:  # noqa: BLE001 - miss
        return None


def _cache_put(key: str | None, body: dict) -> None:
    if CACHE_ON and key and cacheable(body):
        try:
            _blob(key).upload_blob(json.dumps(body, default=str), overwrite=True)
        except Exception:  # noqa: BLE001
            logging.warning("cache write failed")


def _record_cost(r: dict, uid: str | None, session: str | None, seconds: float, route: str) -> dict:
    """Add the Function App's own compute, log one COST_EVENT, add to the user's daily spend."""
    from txfr.finops import PRICES
    cost = r.get("cost") or {"usage": {}, "usd": {"total": 0.0}}
    gb_s = 2.0 * seconds / 4   # 2 GB instance shared by up to 4 concurrent requests (Bicep perInstanceConcurrency)
    cost["usage"]["function_gb_s"] = round(gb_s, 2)
    cost["usd"]["functions"] = round(gb_s * PRICES["functions_per_gb_s"], 6)
    cost["usd"]["total"] = round(sum(v for k, v in cost["usd"].items() if k != "total"), 6)
    _add_spend(uid, cost["usd"]["total"])
    logging.warning("COST_EVENT %s", json.dumps({
        "user": pid(uid), "session": session, "route": route, "seconds": round(seconds, 2), "status": r.get("status"),
        **{f"usd_{k}": v for k, v in cost["usd"].items()},
        "agent_prompt": cost["usage"].get("agent_prompt"), "cs_records": cost["usage"].get("content_safety_records"),
        **{f"t_{k}": v for k, v in (r.get("timing") or {}).items()}}))
    return cost


def _user(req: func.HttpRequest) -> tuple[str | None, str | None]:
    return req.headers.get("x-ms-client-principal-id"), req.headers.get("x-ms-client-principal-name")


@app.route(route="", methods=["GET"])
def page(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(PAGE, mimetype="text/html", headers={"Cache-Control": "no-store",
                                                                  "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:"})


PRIVACY = (Path(__file__).parent / "static" / "privacy.html").read_text()


@app.route(route="privacy", methods=["GET"])
def privacy(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(PRIVACY, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route(route="api/feedback", methods=["POST"])
def feedback(req: func.HttpRequest) -> func.HttpResponse:
    """Explicit sentiment only: thumbs up/down and an optional comment the user chose to write (≤ 500 chars).
    answer_ref (a kept thread id) is present only if the user opted in to keeping that question."""
    try:
        b = req.get_json() or {}
    except ValueError:
        b = {}
    rating = b.get("rating")
    if rating not in ("up", "down"):
        return func.HttpResponse(json.dumps({"error": "rating must be up or down"}), status_code=400, mimetype="application/json")
    uid, _ = _user(req)
    comment = str(b.get("comment") or "").strip()[:500] or None
    logging.warning("FEEDBACK_EVENT %s", json.dumps({"user": pid(uid), "rating": rating, "comment": comment,
                                                      "answer_ref": str(b.get("answer_ref") or "")[:64] or None,
                                                      "cached": bool(b.get("cached"))}))
    return func.HttpResponse(json.dumps({"ok": True}), mimetype="application/json")


@app.route(route="api/health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    uid, name = _user(req)
    return func.HttpResponse(json.dumps({"ok": True, "signed_in": bool(uid), "user": name}), mimetype="application/json")


@app.route(route="api/ask", methods=["POST"])
def ask(req: func.HttpRequest) -> func.HttpResponse:
    from txfr.agent import ask as agent_ask
    t0 = time.time()
    try:
        q = str((req.get_json() or {}).get("question", "")).strip()
    except ValueError:
        q = ""
    if not q or len(q) > MAX_Q:
        guard_event("bad_request", _user(req)[0], route="ask", length=len(q))
        return func.HttpResponse(json.dumps({"error": f"question must be 1-{MAX_Q} characters"}), status_code=400,
                                 mimetype="application/json")
    uid, _ = _user(req)
    session = (req.headers.get("x-txfr-session") or "")[:64] or None
    # memory is opt-in per request ("Remember my agency" on the page): consent first, and cacheable answers otherwise
    use_memory = os.environ.get("TXFR_MEMORY", "1") == "1" and req.params.get("memory", "0") == "1"
    want_pair = (req.params.get("compare") == "1" or random.random() < SBS_SHARE) and os.environ.get("AGENT_ID_KNOWLEDGE")
    if want_pair:
        if _spent_today(uid) >= DAILY_CAP_USD:
            want_pair = False   # over the cap: fall through to the (free) cache, else the cap message below
        else:
            return _side_by_side(q, uid, session, t0)
    ckey = None if use_memory else _cache_key(q)   # personalized (memory) answers are never cached or served from cache
    hit = _cache_get(ckey)
    if hit:
        logging.warning("COST_EVENT %s", json.dumps({"user": pid(uid), "session": session, "route": "ask_cached",
                                                      "seconds": round(time.time() - t0, 2), "status": "cache_hit",
                                                      "usd_total": 0.0}))
        hit.update({"cached": True, "seconds": round(time.time() - t0, 2),
                    "cost": {"usd": {"total": 0.0}, "usage": {}, "note": "served from today's answer cache"}})
        return func.HttpResponse(json.dumps(hit, default=str), mimetype="application/json")
    # the daily cap applies to answers that cost money: cached answers (above) are free and always served
    spent = _spent_today(uid)
    if spent >= DAILY_CAP_USD:
        guard_event("cost_cap", uid, route="ask", spent=round(spent, 4))
        return func.HttpResponse(json.dumps({"error": "daily limit", "detail": f"Daily usage limit reached "
                                             f"(${DAILY_CAP_USD:.2f} per user). It resets at midnight UTC."}),
                                 status_code=429, mimetype="application/json")
    try:
        keep = req.params.get("improve", "0") == "1"   # opt-in: keep this question and answer for 30 days
        r = agent_ask(q, _conn(), verbose=False, user_scope=uid if (uid and use_memory) else None, keep_thread=keep)
    except Exception as e:  # noqa: BLE001
        _local.conn = None   # a broken connection is the most common cause; reopen next time
        logging.exception("ask failed")
        msg = str(e)
        status = 429 if "429" in msg or "rate limit" in msg.lower() else 500
        if status == 429:
            guard_event("rate_limited", uid, route="ask")
        return func.HttpResponse(json.dumps({"error": type(e).__name__, "detail": msg[:300]}), status_code=status,
                                 mimetype="application/json")
    _guard_events(r, uid, "ask")
    cost = _record_cost(r, uid, session, time.time() - t0, "ask")
    body = {"answer": r["answer"], "status": r["status"], "error": r.get("error"),
            "tool_calls": r["tool_calls"], "guard": r.get("guard"), "memories_used": r.get("memories_used", []),
            "tokens": r.get("tokens"), "seconds": round(time.time() - t0, 2), "cost": cost,
            "kept": bool(keep), "answer_ref": r.get("thread_id") if keep else None,
            "images": r.get("images", [])[:3], "timing": r.get("timing")}
    _cache_put(ckey, {k: v for k, v in body.items() if k not in ("cost", "seconds", "timing")})
    code = 429 if r.get("error") and "rate_limit" in str(r.get("error")).lower() else 200
    return func.HttpResponse(json.dumps(body, default=str), status_code=code, mimetype="application/json")


# ------------------------------------------------------------------ A/B: side-by-side answers + votes
def _side_by_side(q: str, uid: str | None, session: str | None, t0: float) -> func.HttpResponse:
    """Both variants answer (in parallel, both with full guardrails, no memory so neither has an edge); the order on
    the page is random. Which variant is on which side is logged server-side (PAIR_EVENT) before the user votes; the
    vote (PREF_EVENT) carries only the pair id and the side picked, so the page never learns the variant names."""
    from txfr.agent import ask as agent_ask
    from txfr.common import connect_sql
    names = list(VARIANTS)
    random.shuffle(names)

    def run(name):
        conn = connect_sql(retries=4, wait=15)   # a fresh connection per worker thread
        try:
            return agent_ask(q, conn, verbose=False, agent_id=os.environ[VARIANTS[name]], keep_thread=False)
        finally:
            conn.close()
    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(run, names))
    pair_id = uuid.uuid4().hex[:16]
    sides = []
    for name, r in zip(names, results, strict=True):
        _guard_events(r, uid, "ask_pair")
        cost = _record_cost(r, uid, session, time.time() - t0, f"ask_pair:{name}")
        sides.append({"answer": r["answer"], "status": r["status"], "tool_calls": r["tool_calls"], "guard": r.get("guard"),
                      "tokens": r.get("tokens"), "cost": cost, "images": r.get("images", [])[:3]})
    logging.warning("PAIR_EVENT %s", json.dumps({"pair_id": pair_id, "user": pid(uid), "session": session,
                                                  "left": names[0], "right": names[1]}))
    return func.HttpResponse(json.dumps({"pair_id": pair_id, "sides": sides, "seconds": round(time.time() - t0, 2)},
                                        default=str), mimetype="application/json")


@app.route(route="api/vote", methods=["POST"])
def vote(req: func.HttpRequest) -> func.HttpResponse:
    try:
        b = req.get_json() or {}
    except ValueError:
        b = {}
    pair_id, choice = str(b.get("pair_id", ""))[:32], b.get("choice")
    if not pair_id or choice not in ("left", "right", "tie"):
        return func.HttpResponse(json.dumps({"error": "pair_id and choice (left|right|tie) required"}), status_code=400,
                                 mimetype="application/json")
    uid, _ = _user(req)
    logging.warning("PREF_EVENT %s", json.dumps({"pair_id": pair_id, "user": pid(uid), "choice": choice}))
    return func.HttpResponse(json.dumps({"ok": True}), mimetype="application/json")


# ------------------------------------------------------------------ Phase F2: remote MCP server (txfr.mcp_server)
@app.route(route="mcp", methods=["POST", "GET", "DELETE"])
def mcp(req: func.HttpRequest) -> func.HttpResponse:
    """MCP streamable HTTP, stateless, JSON responses only (no server-initiated stream: GET → 405)."""
    from txfr.mcp_server import handle
    if req.method != "POST":
        return func.HttpResponse(status_code=405, headers={"Allow": "POST"})
    try:
        msg = req.get_json()
    except ValueError:
        return func.HttpResponse(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}),
                                 status_code=400, mimetype="application/json")
    uid, _ = _user(req)
    msgs = msg if isinstance(msg, list) else [msg]
    out = [r for r in (handle(m, _conn, pid(uid) or "anonymous") for m in msgs) if r is not None]   # pseudonymous id only
    if not out:
        return func.HttpResponse(status_code=202)
    return func.HttpResponse(json.dumps(out if isinstance(msg, list) else out[0], default=str), mimetype="application/json")


# ------------------------------------------------------------------ nightly maintenance (after the 11:30 PM CT pipeline)
@app.timer_trigger(schedule="0 30 6 * * *", arg_name="timer", run_on_startup=False)   # 06:30 UTC = 00:30 CST / 01:30 CDT
def maintenance(timer: func.TimerRequest) -> None:
    """1. Privacy retention: delete kept questions past their date.
    2. Power BI: reload the push dataset from the warehouse (TXFR_PBI_REFRESH=1; needs the app identity in the workspace
       and the tenant setting that lets service principals use Power BI APIs, docs/RUNBOOK.md)."""
    out = {}
    try:
        from txfr.retention import run
        out["retention"] = run(delete=True)
    except Exception as e:  # noqa: BLE001
        out["retention_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    if os.environ.get("TXFR_PBI_REFRESH") == "1":
        try:
            from txfr.powerbi import publish
            _, did = publish()
            out["powerbi"] = "refreshed"
        except BaseException as e:  # noqa: BLE001 - publish() raises SystemExit on API errors
            out["powerbi_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    logging.warning("OPS_EVENT %s", json.dumps({"job": "maintenance", **out}, default=str))
