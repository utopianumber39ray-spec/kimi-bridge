#!/usr/bin/env python3
"""Restore saved NVIDIA cookies into a freshly started headless Chromium.

Run as ExecStartPost of nv-playground-browser.service: waits for CDP, injects
the cookies captured by nv-cookie-snapshot.py (session cookies included), then
loads the playground so the hCaptcha widget comes up in a logged-in context.
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Conn  # noqa: E402

SRC = os.environ.get("NV_COOKIE_FILE", "/home/ai/.dsh/nvidia-session-cookies.json")
PAGE = "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"


def wait_cdp(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=3):
                return True
        except Exception:
            time.sleep(1)
    return False


def main():
    if not os.path.exists(SRC):
        print("no cookie snapshot; skipping restore")
        return 0
    if not wait_cdp():
        print("CDP never came up")
        return 1
    data = json.load(open(SRC))
    c = Conn()
    c.call("Network.enable")
    c.call("Page.enable")
    c.call("Page.navigate", url=PAGE)
    time.sleep(4)
    ok = bad = 0
    for ck in data.get("cookies", []):
        params = {
            "name": ck["name"], "value": ck["value"], "domain": ck["domain"],
            "path": ck.get("path") or "/", "secure": bool(ck.get("secure")),
            "httpOnly": bool(ck.get("httpOnly")),
        }
        if ck.get("sameSite") in ("Strict", "Lax", "None"):
            params["sameSite"] = ck["sameSite"]
        if ck.get("expires", -1) and ck["expires"] > 0:
            params["expires"] = ck["expires"]
        r = c.call("Network.setCookie", **params)
        if r.get("result", {}).get("success"):
            ok += 1
        else:
            bad += 1
    c.call("Page.navigate", url=PAGE)
    print(f"restored {ok} cookies ({bad} rejected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
