#!/usr/bin/env python3
"""Hold the browser's emulation overrides open and keep it looking attended.

- Chromium drops Emulation.* overrides when the CDP client that set them
  disconnects; this daemon keeps one session attached and re-applies them.
- Headless tabs report document.hasFocus() === false, which is a tell AND makes
  hCaptcha more suspicious; focus emulation fixes it.
- Light, human-shaped mouse/scroll activity (real CDP input events) every
  20-45 s keeps the session warm and reduces challenge frequency.
"""
import json
import random
import sys
import time
import urllib.request

import websocket

CDP = "http://127.0.0.1:9333"
W, H = 1440, 900
IDLE_MIN, IDLE_MAX = 20, 45


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} nv-browser-guard {msg}", flush=True)


def page_ws():
    with urllib.request.urlopen(CDP + "/json/list", timeout=10) as r:
        tabs = json.load(r)
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        raise RuntimeError("no page target")
    hit = [t for t in pages if "build.nvidia.com" in t.get("url", "")]
    return (hit or pages)[0]["webSocketDebuggerUrl"]


class Session:
    def __init__(self):
        self.ws = websocket.create_connection(page_ws(), timeout=20, suppress_origin=True)
        self.i = 0
        # 1) renderer-side: viewport + focus
        self.call("Emulation.setDeviceMetricsOverride", width=W, height=H,
                  screenWidth=W, screenHeight=H, deviceScaleFactor=1, mobile=False)
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        try:
            self.call("Page.setWebLifecycleState", state="active")
        except Exception:
            pass

    def call(self, method, timeout=15, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(method)

    def humanize(self):
        x, y = random.randint(200, W - 200), random.randint(150, H - 200)
        steps = random.randint(3, 7)
        for _ in range(steps):
            x += random.randint(-40, 40)
            y += random.randint(-30, 30)
            x = max(10, min(W - 10, x))
            y = max(10, min(H - 10, y))
            self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y,
                      button="none", buttons=0, pointerType="mouse")
            time.sleep(random.uniform(0.05, 0.18))
        if random.random() < 0.35:
            self.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y,
                      deltaX=0, deltaY=random.choice([-120, 120]), pointerType="mouse")
        try:
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        except Exception:
            pass

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main():
    s = None
    while True:
        try:
            if s is None:
                s = Session()
                log(f"attached; viewport {W}x{H}, focus emulated")
            time.sleep(random.uniform(IDLE_MIN, IDLE_MAX))
            s.humanize()
        except Exception as e:  # noqa: BLE001
            log(f"connection issue ({type(e).__name__}); reattaching")
            if s is not None:
                s.close()
            s = None
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
