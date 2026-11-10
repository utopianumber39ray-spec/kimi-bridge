#!/usr/bin/env python3
"""Watch the Kimi bridge and its background NVIDIA Playground page.

If the browser/CDP/page or the bridge health probe fails, restart the browser
first (so its logged-in playground page is recreated), then restart the bridge.
No DSH service or Cloudflare tunnel is touched.
"""
import json
import os
import subprocess
import time
import urllib.request

CDP = "http://127.0.0.1:9333"
BROWSER = "nv-playground-browser.service"
BRIDGE = "kimi-playground.service"
BRIDGE_URL = "http://127.0.0.1:8120/v1/health"
PAGE_PATH = "/moonshotai/kimi-k3_1-pro/playground"


def ctl(*args):
    env = dict(os.environ, XDG_RUNTIME_DIR=os.environ.get(
        "XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid()))
    return subprocess.run(["systemctl", "--user", *args], env=env,
                          capture_output=True, text=True, timeout=120)


def page_ok():
    try:
        with urllib.request.urlopen(CDP + "/json/list", timeout=6) as r:
            tabs = json.load(r)
    except Exception:
        return False
    return any(
        t.get("type") == "page"
        and PAGE_PATH in t.get("url", "")
        for t in tabs
    )


def bridge_ok():
    try:
        with urllib.request.urlopen(BRIDGE_URL, timeout=6) as r:
            return bool(json.load(r).get("ok"))
    except Exception:
        return False


def restart_pair():
    print("Restarting background browser and Kimi bridge", flush=True)
    ctl("reset-failed", BROWSER, BRIDGE)
    ctl(BROWSER, "restart")
    deadline = time.time() + 120
    while time.time() < deadline and not page_ok():
        time.sleep(2)
    ctl("restart", BRIDGE)
    for _ in range(30):
        if page_ok() and bridge_ok():
            print("recovered: browser page and bridge healthy")
            return 0
        time.sleep(2)
    print("recovery incomplete; check systemctl --user status nv-playground-browser kimi-playground")
    return 1


def main():
    if not page_ok() or not bridge_ok():
        print("browser or bridge unhealthy; restarting both")
        ctl("reset-failed", BROWSER, BRIDGE)
        ctl("restart", BROWSER)
        deadline = time.time() + 120
        while time.time() < deadline and not page_ok():
            time.sleep(2)
        ctl("restart", BRIDGE)
        for _ in range(30):
            if page_ok() and bridge_ok():
                print("recovered: background page and bridge healthy")
                return 0
            time.sleep(2)
        print("recovery incomplete; check service logs")
        return 1
    print("ok: bridge and background NVIDIA page healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
