#!/usr/bin/env python3
"""Tiny CDP driver for the headless Chromium on :9333."""
import json
import os
import sys
import time
import urllib.request

import websocket

CDP = os.environ.get("KIMI_CDP", "http://127.0.0.1:9333")


def _tabs():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        return json.load(r)


def page_ws(match=None):
    pages = [t for t in _tabs() if t.get("type") == "page"]
    if match:
        hit = [t for t in pages if match in t["url"]]
        pages = hit or pages
    if not pages:
        req = urllib.request.Request(CDP + "/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)["webSocketDebuggerUrl"]
    return pages[0]["webSocketDebuggerUrl"]


class Conn:
    def __init__(self, match=None):
        self.ws = websocket.create_connection(page_ws(match), timeout=60, suppress_origin=True)
        self.i = 0
        self.events = []

    def pump(self, seconds):
        """Collect CDP events (not responses) for the given duration."""
        end = time.time() + seconds
        self.ws.settimeout(0.5)
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            if "id" in msg:
                continue
            self.events.append(msg)
        self.ws.settimeout(60)
        return len(self.events)

    def call(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + 90
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(method)

    def js(self, expr, await_promise=False):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise, userGesture=True)
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"error": res["exceptionDetails"].get("text"),
                    "detail": str(res["exceptionDetails"].get("exception", {}))[:400]}
        return res.get("result", {}).get("value")

    def goto(self, url, wait=3.0):
        self.call("Page.enable")
        self.call("Page.navigate", url=url)
        time.sleep(wait)
        return self.url()

    def url(self):
        r = self.call("Runtime.evaluate", expression="location.href", returnByValue=True)
        return r["result"]["result"].get("value")

    def shot(self, path):
        r = self.call("Page.captureScreenshot", format="png")
        data = r["result"]["data"]
        import base64
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path


def main():
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    c = Conn()
    if cmd == "goto":
        print(c.goto(arg))
    elif cmd == "js":
        print(json.dumps(c.js(arg), ensure_ascii=False, indent=1)[:4000])
    elif cmd == "shot":
        print(c.shot(arg))
    elif cmd == "text":
        print(c.js("document.body ? document.body.innerText.slice(0,6000) : 'no body'"))
    elif cmd == "url":
        print(c.url())
    else:
        raise SystemExit("unknown cmd")


if __name__ == "__main__":
    main()
