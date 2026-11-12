from txfr import mcp_server


def test_initialize_and_list():
    r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}}, None)
    assert r["result"]["protocolVersion"] == "2025-06-18" and "tools" in r["result"]["capabilities"]
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, None) is None
    names = [t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, None)["result"]["tools"]]
    assert names == ["get_metrics", "search_fiscal_notes", "bill_detail"]


def test_unknown_method_and_tool():
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, None)["error"]["code"] == -32601
    r = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "drop_tables"}}, None, "u")
    assert r["result"]["isError"]


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(mcp_server, "RATE_PER_MIN", 2)
    mcp_server._calls.clear()
    assert not mcp_server._limited("x") and not mcp_server._limited("x") and mcp_server._limited("x")
