#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcp_probe.py — 最小 MCP 客户端（M3.2 验证）：对 UE 编辑器自带的 ModelContextProtocol
HTTP 服务器走一遍真实握手：initialize → tools/list → tools/call。

只用标准库。传输是 streamable HTTP：请求 POST 到 /mcp，响应可能是 application/json，
也可能是 text/event-stream（此时按 SSE 帧取出 data: 行里的 JSON）。

用法：python -X utf8 tools/mcp_probe.py [--url http://127.0.0.1:8000/mcp] [--tool SUBSTR] [--call NAME JSON]
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

MCP_PROTOCOL = "2025-06-18"


def parse_body(content_type, raw):
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        out = []
        for block in text.split("\n\n"):
            data = []
            for line in block.splitlines():
                if line.startswith("data:"):
                    data.append(line[5:].strip())
            if data:
                joined = "\n".join(data)
                try:
                    out.append(json.loads(joined))
                except json.JSONDecodeError:
                    pass
        return out[0] if out else None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


class McpClient:
    def __init__(self, url, timeout=60):
        self.url = url
        self.timeout = timeout
        self.session_id = None
        self.next_id = 1

    def post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("MCP-Protocol-Version", MCP_PROTOCOL)
        if self.session_id:
            req.add_header("Mcp-Session-Id", self.session_id)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.session_id = resp.headers.get("Mcp-Session-Id") or self.session_id
                return resp.status, resp.headers.get("Content-Type", ""), parse_body(
                    resp.headers.get("Content-Type", ""), resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, exc.headers.get("Content-Type", ""), body

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        return self.post(payload)

    def request(self, method, params=None):
        rid = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        status, ctype, body = self.post(payload)
        return status, ctype, body


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="M3.2 MCP 通路验证")
    ap.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    ap.add_argument("--filter", default="PerfEvidence", help="tools/list 里只报告名字含此子串的工具")
    ap.add_argument("--call", nargs=2, metavar=("TOOL", "JSON"),
                    help="调用一个工具，例如 --call <name> '{\"TracePath\": \"D:/x.utrace\"}'")
    a = ap.parse_args(argv)

    c = McpClient(a.url)
    status, ctype, body = c.request("initialize", {
        "protocolVersion": MCP_PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "perfctl-mcp-probe", "version": "0.1"},
    })
    print("[mcp] initialize -> %s (%s)" % (status, ctype))
    if not isinstance(body, dict) or "result" not in body:
        print("[mcp] FAILED initialize body: %r" % (body,))
        return 1
    info = body["result"].get("serverInfo", {})
    print("[mcp] server: %s %s, protocol %s"
          % (info.get("name"), info.get("version"), body["result"].get("protocolVersion")))
    print("[mcp] session: %s" % c.session_id)

    c.notify("notifications/initialized")

    status, ctype, body = c.request("tools/list")
    if not isinstance(body, dict) or "result" not in body:
        print("[mcp] FAILED tools/list: %r" % (body,))
        return 1
    tools = body["result"].get("tools", [])
    hits = [t for t in tools if a.filter.lower() in t.get("name", "").lower()]
    print("[mcp] tools total=%d, matching %r=%d" % (len(tools), a.filter, len(hits)))
    for t in hits:
        print("        %s" % t.get("name"))
        desc = (t.get("description") or "").strip().splitlines()
        if desc:
            print("          %s" % desc[0][:100])

    if a.call:
        name, raw_args = a.call
        args = json.loads(raw_args)
        print("[mcp] tools/call %s args=%s" % (name, json.dumps(args, ensure_ascii=False)))
        status, ctype, body = c.request("tools/call", {"name": name, "arguments": args})
        print("[mcp] -> %s" % json.dumps(body, ensure_ascii=False)[:800])
        if not isinstance(body, dict) or "result" not in body:
            return 1
    return 0 if hits else 2


if __name__ == "__main__":
    sys.exit(main())
