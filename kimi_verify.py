#!/usr/bin/env python3
"""End-to-end verification of the kimi-playground bridge.

Checks:
  1. tool definitions reach the model (it can enumerate them)
  2. nested/enum schemas are understood and called correctly
  3. reasoning content is streamed back
  4. parallel tool calls get distinct ids and valid JSON args
  5. an OpenAI/pi-ai-style accumulator assembles exactly what DSH expects
"""
import json
import sys

import requests

BASE = "http://127.0.0.1:8120/v1/chat/completions"
MODEL = "moonshotai/kimi-k3.1-pro"


def tool(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    tool("bash", "Run a shell command", {"command": {"type": "string"}}, ["command"]),
    tool("read", "Read a file", {"path": {"type": "string"}, "offset": {"type": "integer"}}, ["path"]),
    tool("write", "Write a file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    tool("edit", "Edit a file", {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, ["path", "old", "new"]),
    tool("glob", "Find files by glob", {"pattern": {"type": "string"}}, ["pattern"]),
    tool("grep", "Search file contents", {"pattern": {"type": "string"}, "include": {"type": "string"}}, ["pattern"]),
    tool("web_fetch", "Fetch a URL", {"url": {"type": "string"}}, ["url"]),
    tool("web_search", "Search the web", {"queries": {"type": "array", "items": {"type": "string"}}}, ["queries"]),
    tool("todo_write", "Write the task list", {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}}}}, ["todos"]),
    tool("deploy", "Deploy a service", {
        "env": {"type": "string", "enum": ["dev", "staging", "prod"]},
        "flags": {"type": "object", "properties": {"retries": {"type": "integer"}, "tags": {"type": "array", "items": {"type": "string"}}}},
        "note": {"type": "string"}}, ["env"]),
    tool("send_message", "Message a teammate", {"agent_id": {"type": "string"}, "message": {"type": "string"}}, ["agent_id", "message"]),
    tool("skill", "Load a skill", {"id": {"type": "string"}}, ["id"]),
]


def stream(messages, tools=True, effort="low", max_tokens=700):
    body = {"model": MODEL, "messages": messages, "max_tokens": max_tokens,
            "reasoning_effort": effort, "stream": True}
    if tools:
        body["tools"] = TOOLS
    r = requests.post(BASE, json=body, stream=True, timeout=300)
    r.raise_for_status()
    calls, reasoning, content, finish = {}, [], [], None
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if p == "[DONE]":
            break
        try:
            o = json.loads(p)
        except Exception:
            continue
        for ch in o.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            if d.get("content"):
                content.append(d["content"])
            for tc in d.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0),
                                        {"id": "", "name": "", "args": ""})
                if tc.get("id") and not slot["id"]:
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name") and not slot["name"]:
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    return {"calls": calls, "reasoning": "".join(reasoning),
            "content": "".join(content), "finish": finish}


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + (("  -> " + detail) if detail else ""))
    return ok


def main():
    results = []

    # 1. can the model see every tool definition?
    r = stream([{"role": "user",
                 "content": "List the exact names of every tool you can call, comma-separated, nothing else."}],
               tools=True)
    names = {t["function"]["name"] for t in TOOLS}
    seen = {x.strip() for x in r["content"].replace("\n", ",").split(",") if x.strip()}
    results.append(check("all 12 tool names reach the model", names <= seen,
                         "missing=%s" % sorted(names - seen)))
    results.append(check("reasoning streamed", len(r["reasoning"]) > 20,
                         "%d chars" % len(r["reasoning"])))

    # 2. nested + enum schema call
    r = stream([
        {"role": "user", "content": "Deploy to prod with retries=3, tags alpha and beta. Use the deploy tool."},
    ])
    calls = r["calls"]
    ok = bool(calls)
    args = {}
    if ok:
        c = calls[0]
        try:
            args = json.loads(c["args"])
        except Exception:
            ok = False
        ok = ok and c["name"] == "deploy" and c["id"].startswith("call_")
        ok = ok and args.get("env") == "prod"
        ok = ok and (args.get("flags") or {}).get("retries") == 3
        ok = ok and set((args.get("flags") or {}).get("tags") or []) == {"alpha", "beta"}
    results.append(check("nested/enum args are valid JSON and correct", ok,
                         json.dumps(args)[:160]))

    # 3. parallel calls with distinct ids
    r = stream([{"role": "user",
                 "content": "Call bash with command 'echo A' and read with path '/tmp/x' in parallel. Use both tools."}])
    calls = r["calls"]
    ids = [c["id"] for c in calls.values()]
    names_called = {c["name"] for c in calls.values()}
    ok = len(calls) == 2 and len(set(ids)) == 2 and {"bash", "read"} <= names_called
    ok = ok and all(c["id"].startswith("call_") for c in calls.values())
    args_ok = True
    for c in calls.values():
        try:
            json.loads(c["args"])
        except Exception:
            args_ok = False
    results.append(check("two parallel calls, distinct ids, valid JSON", ok and args_ok,
                         "names=%s ids=%s" % (sorted(names_called), ids)))

    # 4. finish_reason + no duplicate names
    r = stream([{"role": "user", "content": "Use bash to echo hi."}])
    c = next(iter(r["calls"].values()))
    ok = r["finish"] == "tool_calls" and c["name"] == "bash" and c["args"] == '{"command":"echo hi"}'
    results.append(check("finish=tool_calls, single clean name+args", ok,
                         "%r %r" % (c["name"], c["args"])))

    print("\n%d/%d checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
