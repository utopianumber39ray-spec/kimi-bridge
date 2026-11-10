#!/usr/bin/env python3
"""Harden the headless NVIDIA browser: stealth shims, consistent screen/timezone,
UA without "HeadlessChrome", then restore saved cookies and open the playground.

Everything here is applied once per browser start and survives page reloads
(Emulation overrides and addScriptToEvaluateOnNewDocument are target-scoped).
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
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/156.0.0.0 Safari/537.36")
TZ = os.environ.get("NV_TZ", "Asia/Baghdad")
W, H = 1440, 900

STEALTH = r"""
(() => {
  if (window.__nvHarden) return;
  window.__nvHarden = 1;
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined, configurable: true
    });
  } catch (e) {}
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {};
    if (!window.chrome.app) window.chrome.app = {isInstalled: false};
    if (!window.chrome.csi) window.chrome.csi = function () { return {}; };
    if (!window.chrome.loadTimes) window.chrome.loadTimes = function () { return {}; };
  } catch (e) {}
  try {
    const spoof = {
      37445: 'Google Inc. (Intel)',
      37446: 'ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)'
    };
    const orig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p in spoof) return spoof[p];
      return orig.apply(this, arguments);
    };
    const orig2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (p) {
      if (p in spoof) return spoof[p];
      return orig2.apply(this, arguments);
    };
  } catch (e) {}
  try {
    const q = navigator.permissions && navigator.permissions.query;
    if (q) {
      navigator.permissions.query = function (p) {
        if (p && p.name === 'notifications') {
          return Promise.resolve({state: Notification.permission, onchange: null});
        }
        return q.apply(navigator.permissions, arguments);
      };
    }
  } catch (e) {}
})();
"""


def wait_cdp(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:9333/json/version", timeout=3):
                return True
        except Exception:
            time.sleep(1)
    return False


def main():
    if not wait_cdp():
        print("CDP never came up")
        return 1
    c = Conn()
    c.call("Network.enable")
    c.call("Page.enable")
    # 1) script that runs before any page script on every new document
    c.call("Page.addScriptToEvaluateOnNewDocument", source=STEALTH)
    # 2) consistent UA / platform
    c.call("Network.setUserAgentOverride", userAgent=UA, acceptLanguage="en-US,en",
           platform="Linux x86_64")
    # 3) screen == window, so screen.width/height are not the 800x600 headless
    #    default. Applied *after* navigation: device-metrics emulation lives in
    #    the renderer and is dropped by a cross-site navigation.
    def set_metrics():
        try:
            c.call("Emulation.setDeviceMetricsOverride", width=W, height=H,
                   screenWidth=W, screenHeight=H, deviceScaleFactor=1, mobile=False)
        except Exception as e:  # noqa: BLE001
            print("metrics override failed:", e)
    # 4) local timezone instead of UTC
    try:
        c.call("Emulation.setTimezoneOverride", timezoneId=TZ)
    except Exception as e:  # noqa: BLE001
        print("timezone override failed:", e)
    # 5) cookies from the last snapshot (NVIDIA's SID etc. are session cookies)
    ok = bad = 0
    if os.path.exists(SRC):
        data = json.load(open(SRC))
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
    # 6) open the playground, then pin the viewport on the final page
    c.call("Page.navigate", url=PAGE)
    time.sleep(6)
    set_metrics()
    print(f"hardened: ua/stealth/metrics/tz={TZ}, cookies restored {ok} ({bad} rejected)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
