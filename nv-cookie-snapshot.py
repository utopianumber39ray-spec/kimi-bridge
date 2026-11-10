#!/usr/bin/env python3
"""Snapshot every NVIDIA cookie (session cookies included) from the live browser.

NVIDIA's login cookies (SID, __ngc_ui, MainAuth_*) are *session* cookies, so a
clean browser exit would delete them. This dump lets nv-cookie-restore.py put
them back after a restart, so the Playground session survives.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Conn  # noqa: E402

OUT = os.environ.get("NV_COOKIE_FILE", "/home/ai/.dsh/nvidia-session-cookies.json")
KEEP = ("nvidia", "ngc")


def main():
    c = Conn()
    r = c.call("Storage.getCookies")
    cookies = r.get("result", {}).get("cookies", [])
    picked = []
    for ck in cookies:
        dom = ck.get("domain", "")
        if not any(k in dom for k in KEEP):
            continue
        picked.append({
            "name": ck.get("name"),
            "value": ck.get("value"),
            "domain": dom,
            "path": ck.get("path", "/"),
            "secure": ck.get("secure", False),
            "httpOnly": ck.get("httpOnly", False),
            "sameSite": ck.get("sameSite"),
            "expires": ck.get("expires", -1),
        })
    tmp = OUT + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"saved_at": int(time.time()), "cookies": picked}, f, indent=1)
    os.replace(tmp, OUT)
    session = sum(1 for p in picked if p["expires"] in (-1, None, 0))
    print(f"saved {len(picked)} cookies ({session} session-only) -> {OUT}")


if __name__ == "__main__":
    main()
