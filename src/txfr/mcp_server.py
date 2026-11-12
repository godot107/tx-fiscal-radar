"""Phase F2: a remote MCP server so external AI agents (Claude, Copilot, ChatGPT, a newsroom's own tools) can use the
same facts as Fiscal Note Desk. Served by the Function App at POST /mcp (MCP "streamable HTTP", JSON responses).

Design: expose the deterministic tools, not our agent. The caller's model does the reasoning (and pays for it);
this server returns auditable facts with sources:
    get_metrics          one of the fixed, read-only warehouse queries (no free-form SQL)
    search_fiscal_notes  hybrid + semantic search over bill cards, agency cards and fiscal-note text, with links;
                         each result is screened by Prompt Shields first, so planted instructions in a document don't
                         reach the caller's agent either
    bill_detail          one bill's status and latest fiscal-note figures
Auth: the Function App's Entra sign-in (a bearer token for api://<APP_CLIENT_ID>). Rate limit: per user per
instance (production: API Management with per-subscription quotas).

Only the methods a tool server needs: initialize, notifications/initialized, ping, tools/list, tools/call.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "tx-fiscal-radar", "title": "Texas Fiscal Radar (fiscal notes + warehouse)", "version": "1.0.0"}
INSTRUCTIONS = ("Facts about Texas bills and Legislative Budget Board fiscal notes (2025 sessions 89R/891/892; 2027 "
                "session 90R in prefiling). Numbers come from fixed warehouse queries; explanations from fiscal-note text "
                "with source URLs. Fiscal notes are estimates of a bill version, not appropriations. Cite source_url. "
                "Privacy: tool calls are logged with a pseudonymous user id (no query text) for 30 days; see /privacy.")
RATE_PER_MIN = 60
_calls: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def tools() -> list[dict]:
    from txfr.agent import FUNCTION_SPEC, METRICS
    metrics_schema = FUNCTION_SPEC["function"]["parameters"]
    return [
        {"name": "get_metrics", "title": "Warehouse metric",
         "description": "Run one fixed, read-only query against the Texas fiscal warehouse. Metrics:\n" +
                        "\n".join(f"- {k}({', '.join(v[2])}): {v[0]}" for k, v in METRICS.items()),
         "inputSchema": metrics_schema, "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "search_fiscal_notes", "title": "Search fiscal notes",
         "description": "Search bill cards, agency cards and fiscal-note text by meaning and keywords (everyday wording "
                        "works). Returns up to 6 documents with title, session, bill, content and source_url.",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"},
             "session": {"type": "string", "enum": ["89R", "891", "892", "90R"], "description": "89R = 2025 regular session"},
             "doc_type": {"type": "string", "enum": ["bill_card", "agency_card", "note_section"]},
             "agency_code": {"type": "string", "description": "3-digit state agency code"}}, "required": ["query"]},
         "annotations": {"readOnlyHint": True, "openWorldHint": False}},
        {"name": "bill_detail", "title": "Bill detail",
         "description": "One bill's status and latest fiscal-note figures (2026-27 cost, General Revenue, FTEs, agencies).",
         "inputSchema": {"type": "object", "properties": {
             "session_code": {"type": "string", "enum": ["89R", "891", "892", "90R"]},
             "bill": {"type": "string", "description": "e.g. HB2, SB 2206"}}, "required": ["session_code", "bill"]},
         "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    ]


def _limited(user: str) -> bool:
    now = time.time()
    with _lock:
        q = _calls[user]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MIN:
            return True
        q.append(now)
        return False


def call_tool(name: str, args: dict, conn_factory, user: str) -> dict:
    """`user` is the caller's pseudonymous id (function_app.pid), never the raw Entra id."""
    from txfr import agent
    if _limited(user):
        import logging
        logging.warning("GUARD_EVENT %s", json.dumps({"kind": "rate_limited", "user": user, "route": "mcp"}))
        return {"content": [{"type": "text", "text": f"Rate limit: {RATE_PER_MIN} calls per minute. Try again shortly."}],
                "isError": True}
    try:
        if name == "get_metrics":
            out = agent.get_metrics(conn_factory(), **args)
        elif name == "bill_detail":
            out = agent.get_metrics(conn_factory(), "bill_detail", **args)
        elif name == "search_fiscal_notes":
            guard = {"docs_removed": 0}
            out = agent._shield_search(args.get("query", ""), agent.search_session(**args), guard)
            if guard["docs_removed"]:
                import logging
                logging.warning("GUARD_EVENT %s", json.dumps({"kind": "injection_removed", "user": user, "route": "mcp",
                                                               "docs": guard["docs_removed"]}))
        else:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    except (TypeError, ValueError) as e:   # bad or missing arguments
        return {"content": [{"type": "text", "text": f"invalid arguments: {e}"}], "isError": True}
    except Exception as e:  # noqa: BLE001 - a tool error is a result for the caller, not an HTTP 500
        import logging
        logging.exception("mcp tool %s failed", name)
        return {"content": [{"type": "text", "text": f"tool error: {type(e).__name__}"}], "isError": True}
    data = json.loads(out)
    import logging   # usage record: pseudonymous user + tool name, no arguments or query text
    logging.warning("MCP_EVENT %s", json.dumps({"user": user, "tool": name, "ok": "error" not in data}))
    return {"content": [{"type": "text", "text": out}], "structuredContent": data, "isError": "error" in data}


def handle(msg: dict, conn_factory, user: str = "anonymous") -> dict | None:
    """One JSON-RPC message → response (None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:   # notification (e.g. notifications/initialized): no response
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method == "initialize":
        v = params.get("protocolVersion")
        return ok({"protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO,
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": tools()})
    if method == "tools/call":
        return ok(call_tool(params.get("name"), params.get("arguments") or {}, conn_factory, user))
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
