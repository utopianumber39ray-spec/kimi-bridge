#!/usr/bin/env python3
"""Kimi Code MCP adapter for the local SearXNG search service.

SearXNG is free, self-hosted metasearch (currently at 127.0.0.1:8888). It is
not an unlimited upstream API: upstream engines still have their own limits.
No API key is required for this local instance.
"""
import sys
import os
import json
import urllib.parse
import urllib.request

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")


def tool_search(query, count=10):
    count = max(1, min(20, int(count)))
    qs = urllib.parse.urlencode({"q": query, "format": "json"})
    req = urllib.request.Request(SEARXNG_URL + "/search?" + qs, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    items = []
    for item in data.get("results", [])[:count]:
        items.append({
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "content": (item.get("content") or "")[:500],
            "engine": item.get("engine", ""),
        })
    return {"query": query, "source": "searxng-local", "results": items}


TOOLS = [{
    "name": "search",
    "description": "Search via local SearXNG (free self-hosted metasearch). Returns URL, title, snippet, engine.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "count": {"type": "integer", "default": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    }},
]


def call_tool(name, args):
    if name != "search":
        raise RuntimeError(f"unknown tool: {name}")
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query required")
    count = max(1, min(20, int(args.get("count", 10))))
    qs = urllib.parse.urlencode({"q": query, "format": "json"})
    url = SEARXNG_URL + "/search?" + qs
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    out = []
    for item in data.get("results", [])[:count]:
        out.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("content") or "")[:400],
            "engine": item.get("engine", ""),
        })
    return {"query": query, "source": "searxng-local", "results": out}


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if not method:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "searxng-local", "version": "1.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            try:
                params = msg.get("params", {})
                args = params.get("arguments") or {}
                result = call_tool(params.get("name"), args)
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}]}})
            except Exception as e:
                send({"jsonrpc": "2.0", "id": mid,
                      "result": {"isError": True, "content": [{"type": "text", "text": str(e)}]}})


if __name__ == "__main__":
    main()
