#!/usr/bin/env python3
"""OpenAI-compatible bridge exposing NVIDIA Playground's kimi-k3.1-pro.

The playground endpoint (buildapi.ngc.nvidia.com/v2/predict/models/<fn>/kimi-k3_1-pro)
is protected by session cookies + a per-request hCaptcha token, so requests are
executed *inside* the logged-in headless Chromium page via CDP, and the SSE
stream is relayed back verbatim.
"""
import glob
import json
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket

CDP_HTTP = os.environ.get("KIMI_CDP", "http://127.0.0.1:9333")


def _default_models():
    return {
        "moonshotai/kimi-k3.1-pro": {
            "page": "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground",
            "slug": "kimi-k3_1-pro",
            "fnid": "b79234a1-64e1-479e-87e7-1c8e3af74262",
            "context": 262144,
            "max_out": 65536,
        },
    }


MODELS = json.loads(os.environ["KIMI_MODELS"]) if os.environ.get("KIMI_MODELS") else _default_models()
MODEL = os.environ.get("KIMI_MODEL", "moonshotai/kimi-k3.1-pro")
if MODEL not in MODELS:
    MODELS[MODEL] = {
        "page": os.environ.get("KIMI_PAGE_URL", "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"),
        "slug": os.environ.get("KIMI_SLUG", "kimi-k3_1-pro"),
        "fnid": os.environ.get("KIMI_FUNCTION_ID", "b79234a1-64e1-479e-87e7-1c8e3af74262"),
        "context": 262144,
    }
PAGE_URL = MODELS[MODEL]["page"]
UPSTREAM = os.environ.get(
    "KIMI_UPSTREAM",
    "https://buildapi.ngc.nvidia.com/v2/predict/models/qc69jvmznzxy/" + MODELS[MODEL]["slug"],
)
FUNCTION_ID = os.environ.get("KIMI_FUNCTION_ID", MODELS[MODEL]["fnid"])


def resolve_model(name):
    if not name:
        return MODEL, MODELS[MODEL]
    if name in MODELS:
        return name, MODELS[name]
    for mid, cfg in MODELS.items():
        if mid.split("/", 1)[-1] == name or cfg.get("slug") == name:
            return mid, cfg
    return name, None


def upstream_for(cfg):
    return "https://buildapi.ngc.nvidia.com/v2/predict/models/qc69jvmznzxy/" + cfg["slug"]
PORT = int(os.environ.get("KIMI_BRIDGE_PORT", "8120"))
# Kimi streams ~10 tok/s and NVIDIA packs whole tool calls into one chunk, so a
# long turn can legitimately run well past 15 minutes.
TOTAL_TIMEOUT = float(os.environ.get("KIMI_TIMEOUT", "3600"))
IDLE_TIMEOUT = float(os.environ.get("KIMI_IDLE_TIMEOUT", "120"))
MAX_STREAM_BYTES = int(float(os.environ.get("KIMI_MAX_STREAM_MB", "64")) * 1_000_000)
# Upstream slots. Every stream shares one playground tab (and its HTTP/2
# connection to NVIDIA), so one network drop kills all of them at once, and a
# wide fan-out saturates NVIDIA's pool (Pool#acquire 500s). Keep it small.
CONCURRENCY = max(1, int(os.environ.get("KIMI_CONCURRENCY", "6")))
_slots = threading.BoundedSemaphore(CONCURRENCY)
# Per-model admission limits can be added here if a model's upstream queue
# saturates; other models keep the global limit.
PER_MODEL_CONCURRENCY = {}
_model_slots = {}
OVERLOAD_ATTEMPTS = max(1, int(os.environ.get("KIMI_OVERLOAD_ATTEMPTS", "2")))
DUMP_DIR = os.environ.get("KIMI_DUMP_DIR", "")


def dump(kind, data):
    """Optional debug dump, off unless KIMI_DUMP_DIR is explicitly set."""
    if not DUMP_DIR:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        p = os.path.join(DUMP_DIR, "%s-%d.txt" % (kind, int(time.time() * 1000)))
        with open(p, "w", encoding="utf-8") as f:
            f.write(data)
        files = sorted(glob.glob(os.path.join(DUMP_DIR, "*.txt")))
        for old in files[:-60]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception as e:
        log("dump failed: %s" % e)


_lock = threading.Lock()
_mint_lock = threading.Lock()
_pace_lock = threading.Lock()
_pace_next = 0.0
MIN_GAP = float(os.environ.get("KIMI_MIN_GAP", "0.5"))
JITTER = float(os.environ.get("KIMI_JITTER", "0.8"))


def pace():
    """Space requests out with human-ish jitter instead of a tight burst."""
    global _pace_next
    with _pace_lock:
        now = time.time()
        wait = max(0.0, _pace_next - now)
        _pace_next = max(now, _pace_next) + MIN_GAP + random.uniform(0, JITTER)
    if wait > 0:
        time.sleep(wait)


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} kimi-bridge {msg}", flush=True)


class CdpError(RuntimeError):
    pass


class Cdp:
    def __init__(self, page_url=None):
        self.page_url = page_url or PAGE_URL
        with urllib.request.urlopen(CDP_HTTP + "/json/list", timeout=10) as r:
            tabs = json.load(r)
        target = None
        for t in tabs:
            if t.get("type") == "page" and t.get("url", "").startswith(self.page_url.rstrip("/")):
                target = t
                break
        if target is None:
            # Never reuse another model's tab: navigating it would kill its
            # in-flight streams. Open a dedicated tab for this model instead.
            req = urllib.request.Request(
                CDP_HTTP + "/json/new?" + urllib.parse.quote(self.page_url, safe=":/?=&"), method="PUT"
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                target = json.load(r)
        self.ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=15, suppress_origin=True
        )
        self.i = 0
        self.events = []
        self._ensure_page()

    def _ensure_page(self):
        try:
            cur = self.js("location.href", timeout=10)
        except CdpError:
            cur = None
        on_page = isinstance(cur, str) and cur.startswith(self.page_url.rstrip("/"))
        if on_page:
            try:
                if self.js("typeof window.hcaptcha === 'object'", timeout=20) is True:
                    return
            except CdpError:
                pass
        log("navigating playground tab to %s" % self.page_url)
        self.call("Page.enable", timeout=20)
        self.call("Page.navigate", timeout=20, url=self.page_url)
        for _ in range(40):
            time.sleep(1.5)
            try:
                if self.js("typeof window.hcaptcha === 'object'", timeout=10) is True:
                    return
            except CdpError:
                continue
        raise CdpError("playground page did not load hCaptcha")

    def reload_page(self):
        try:
            self.call("Page.enable", timeout=20)
            self.call("Page.navigate", timeout=20, url=self.page_url)
        except CdpError:
            return False
        for _ in range(40):
            time.sleep(1.5)
            try:
                if self.js("typeof window.hcaptcha === 'object'", timeout=10) is True:
                    return True
            except CdpError:
                continue
        return False

    def call(self, method, timeout=60, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CdpError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
            self.events.append(msg)
        raise CdpError(f"cdp no reply for {method}")

    def js(self, expr, await_promise=False, timeout=120):
        r = self.call(
            "Runtime.evaluate",
            timeout=timeout,
            expression=expr,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
        )
        if "exceptionDetails" in r:
            raise CdpError(str(r["exceptionDetails"].get("exception"))[:300])
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


SITEKEY = os.environ.get("KIMI_SITEKEY", "0c6a1e45-75d7-43cc-b836-a0c9d886b8ee")

# One invisible widget per slot, so two tokens can be minted in parallel
# (hCaptcha serialises execute() per widget).
MINT_JS = """
(async () => {
  const K = %d;
  const k = (n) => '__kimi_' + n + '_' + K;
  window[k('tok')] = null;
  window[k('err')] = null;
  if (window[k('w')] === undefined) {
    const d = document.createElement('div');
    d.id = 'kimi-mint-' + K + '-' + Date.now();
    d.style.display = 'none';
    document.body.appendChild(d);
    window[k('w')] = window.hcaptcha.render(d, {
      sitekey: %s,
      size: 'invisible',
      theme: 'dark',
      callback: (t) => { window[k('tok')] = t; },
      'error-callback': (e) => { window[k('err')] = String(e); },
      'expired-callback': () => { window[k('tok')] = null; }
    });
  }
  try { if (window[k('w')] !== undefined) { window.hcaptcha.reset(window[k('w')]); } } catch (e) {}
  try { window.hcaptcha.execute(window[k('w')]); }
  catch (e) { window[k('err')] = 'execute: ' + e; }
  for (let i = 0; i < 160 && !window[k('tok')]; i++) {
    if (window[k('err')]) return null;
    await new Promise((r) => setTimeout(r, 250));
  }
  return window[k('tok')];
})()
""" % (0, json.dumps(SITEKEY))


def mint_js(slot):
    return MINT_JS.replace("const K = 0;", "const K = %d;" % slot, 1)

STREAM_JS = """
(async () => {
  const B = %(binding)s;
  try {
    const r = await fetch(%(url)s, {
      method: 'POST',
      headers: {'accept': 'text/event-stream', 'content-type': 'application/json',
                'nv-captcha-token': %(token)s, 'nv-function-id': %(fnid)s},
      body: %(body)s,
      credentials: 'include'
    });
    if (!r.ok) {
      const t = await r.text();
      window[B]('__ERR__' + r.status + '|' + t.slice(0, 500));
      return;
    }
    const rd = r.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const {done, value} = await rd.read();
      if (done) break;
      window[B](dec.decode(value, {stream: true}));
    }
    window[B]('__END__');
  } catch (e) {
    try { window[B]('__ERR__0|' + String(e)); } catch (_) {}
  }
})()
"""


MAX_IMAGES = int(os.environ.get("KIMI_MAX_IMAGES", "5"))
IMAGE_PLACEHOLDER = "[earlier image omitted to stay within the playground image limit]"

# --- hCaptcha token pre-minting -------------------------------------------------
# Minting costs ~2.5 s, so background threads keep a small pool of tokens warm
# while the session is active (mirrors a human who keeps using the playground).
# Each slot owns its own invisible widget, so up to TOKEN_POOL mints can run in
# parallel — necessary when several dsh sessions fire at once.
TOKEN_TTL = float(os.environ.get("KIMI_TOKEN_TTL", "100"))
ACTIVE_WINDOW = float(os.environ.get("KIMI_ACTIVE_WINDOW", "120"))
TOKEN_POOL = int(os.environ.get("KIMI_TOKEN_POOL", "16"))
_cache_lock = threading.Lock()
_pool = []  # [(token, minted_at, model_id)] oldest first
_last_req = [0.0]
_seen_tools = [False]
_inflight = [0]
_slot_locks = [threading.Lock() for _ in range(max(1, TOKEN_POOL))]


def _mint_once(slot=0, page_url=None):
    cdp = Cdp(page_url)
    try:
        return cdp.js(mint_js(slot), await_promise=True, timeout=180)
    finally:
        cdp.close()


def take_cached_token(model_id=MODEL):
    with _cache_lock:
        now = time.time()
        while _pool and now - _pool[0][1] >= TOKEN_TTL:
            _pool.pop(0)
        for i, (tok, at, mid) in enumerate(_pool):
            if mid == model_id:
                _pool.pop(i)
                log("using pre-minted captcha token for %s (age %.0fs, %d left)" % (mid, now - at, len(_pool)))
                return tok
    return None


def _mint_any(model_id=MODEL, page_url=None):
    """Mint on demand, using any free slot so parallel mints do not serialise."""
    for i, lk in enumerate(_slot_locks):
        if lk.acquire(blocking=False):
            try:
                return _mint_once(i, page_url)
            finally:
                lk.release()
    with _slot_locks[0]:
        return _mint_once(0, page_url)


def _premint_slot(slot):
    if not _slot_locks[slot].acquire(blocking=False):
        with _cache_lock:
            _inflight[0] -= 1
        return
    try:
        tok = _mint_once(slot, PAGE_URL)
        if tok:
            with _cache_lock:
                _pool.append((tok, time.time(), MODEL))
            log("pre-minted captcha token (pool %d/%d)" % (len(_pool), TOKEN_POOL))
    except Exception as e:  # noqa: BLE001
        log(f"premint failed: {e!r}")
    finally:
        with _cache_lock:
            _inflight[0] -= 1
        _slot_locks[slot].release()


def _premint_loop():
    """Continuous hCaptcha execute() calls burn the session reputation, so ALL
    tokens start being rejected. Mint on demand only, like a real user."""
    if os.environ.get("KIMI_PREMINT", "0") != "1":
        log("captcha pre-mint disabled (on-demand only)")
        while True:
            time.sleep(60)
    while True:
        try:
            time.sleep(1.0)
            if time.time() - _last_req[0] > ACTIVE_WINDOW:
                continue
            with _cache_lock:
                now = time.time()
                while _pool and now - _pool[0][1] >= TOKEN_TTL:
                    _pool.pop(0)
                need = TOKEN_POOL - len(_pool) - _inflight[0]
            for slot in range(need):
                if slot < len(_slot_locks) and not _slot_locks[slot].locked():
                    with _cache_lock:
                        _inflight[0] += 1
                    threading.Thread(target=_premint_slot, args=(slot,),
                                     name=f"premint{slot}", daemon=True).start()
        except Exception as e:  # noqa: BLE001
            log(f"premint loop error: {e!r}")
            time.sleep(3)


# NVIDIA rejects any request body over ~10 MB with HTTP 413 ("Content Too
# Large"). Oversized payloads are almost always embedded base64 images or a
# bloated history, so shrink toward the cap instead of failing the whole turn.
# Trimming only kicks in once a request crosses PAYLOAD_TRIGGER (8 MB by
# default); anything smaller is forwarded untouched. The shrink target
# (PAYLOAD_BUDGET, 9 MB) stays below NVIDIA's hard 10 MB limit.
PAYLOAD_TRIGGER = int(os.environ.get("KIMI_PAYLOAD_TRIGGER", str(8 * 1024 * 1024)))
PAYLOAD_BUDGET = int(os.environ.get("KIMI_PAYLOAD_BUDGET", str(9 * 1024 * 1024)))
PAYLOAD_PLACEHOLDER = "[earlier content omitted: request exceeded the playground payload limit]"


def _part_bytes(part):
    if not isinstance(part, dict):
        return 0
    if part.get("type") in ("image_url", "input_image"):
        url = part.get("image_url") or part.get("input_image") or {}
        if isinstance(url, dict):
            url = url.get("url") or ""
        return len(str(url)) + 64
    return len(str(part.get("text") or "")) + 32


def _msg_bytes(msg):
    content = msg.get("content")
    if isinstance(content, list):
        return sum(_part_bytes(p) for p in content) + 64
    return len(str(content or "")) + 64


def fit_payload_budget(body, budget=PAYLOAD_BUDGET, trigger=PAYLOAD_TRIGGER):
    """Shrink `body['messages']` until the serialized request fits `budget`.

    No-op unless the body already exceeds `trigger` (8 MB): below that the
    request is forwarded as-is. Strategy: blank the largest base64 images first
    (newest kept), then drop the oldest history turns. A text placeholder
    preserves conversational context.
    """
    def total():
        try:
            return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        except Exception:
            return budget + 1

    if total() <= trigger:
        return body
    before = total()
    msgs = list(body.get("messages") or [])

    # Pass 1: replace the largest embedded images with a placeholder.
    imgs = []
    for i, m in enumerate(msgs):
        c = m.get("content")
        if isinstance(c, list):
            for j, p in enumerate(c):
                if isinstance(p, dict) and p.get("type") in ("image_url", "input_image"):
                    imgs.append((_part_bytes(p), i, j))
    for size, i, j in sorted(imgs, reverse=True):
        if total() <= budget:
            break
        c = list(msgs[i]["content"])
        c[j] = {"type": "text", "text": PAYLOAD_PLACEHOLDER}
        msgs[i] = {**msgs[i], "content": c}
        body = {**body, "messages": msgs}

    # Pass 2: drop the oldest non-system turns until it fits.
    while total() > budget and len(msgs) > 2:
        for k, m in enumerate(msgs):
            if m.get("role") != "system":
                msgs = msgs[:k] + msgs[k + 1:]
                break
        else:
            break
        body = {**body, "messages": msgs}

    # Pass 3: a single huge text part can exceed the budget on its own; hard-truncate
    # the largest text parts so the serialized body always lands under the cap.
    guard = 0
    while total() > budget and guard < 200:
        guard += 1
        over = total() - budget + 256
        biggest = None
        for i, m in enumerate(msgs):
            c = m.get("content")
            if isinstance(c, str) and len(c) > 512:
                if biggest is None or len(c) > biggest[0]:
                    biggest = (len(c), i, None)
            elif isinstance(c, list):
                for j, p in enumerate(c):
                    if isinstance(p, dict) and p.get("type") == "text" and len(p.get("text") or "") > 512:
                        if biggest is None or len(p["text"]) > biggest[0]:
                            biggest = (len(p["text"]), i, j)
        if biggest is None:
            break
        _, i, j = biggest
        c = msgs[i]["content"]
        if j is None:
            msgs[i] = {**msgs[i], "content": c[:-over] + "\n" + PAYLOAD_PLACEHOLDER}
        else:
            parts = list(c)
            parts[j] = {**parts[j], "text": parts[j]["text"][:-over] + "\n" + PAYLOAD_PLACEHOLDER}
            msgs[i] = {**msgs[i], "content": parts}
        body = {**body, "messages": msgs}

    log("payload trimmed: %d -> %d bytes (budget %d)" % (before, total(), budget))
    return body


def trim_images(messages, limit=MAX_IMAGES):
    """Keep only the newest `limit` images; older ones become a text placeholder."""
    seen = 0
    out = []
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, list):
            new_parts = []
            for part in reversed(content):
                if isinstance(part, dict) and part.get("type") in ("image_url", "input_image"):
                    seen += 1
                    if seen > limit:
                        new_parts.append({"type": "text", "text": IMAGE_PLACEHOLDER})
                        continue
                new_parts.append(part)
            content = list(reversed(new_parts))
            msg = {**msg, "content": content}
        out.append(msg)
    return list(reversed(out))


def build_upstream_body(req, model_id=MODEL, model_cfg=None):
    model_cfg = model_cfg or MODELS.get(model_id) or {}
    src = trim_images(req.get("messages") or [])
    effort = req.get("reasoning_effort") or os.environ.get("KIMI_DEFAULT_EFFORT", "none")
    requested = req.get("max_tokens") or req.get("max_completion_tokens")
    model_cap = int(model_cfg.get("max_out") or 65536)
    requested = min(int(requested or model_cap), model_cap)
    # Ceiling only: the model may stop normally after any amount. Giving it the
    # whole NVIDIA allowance lets long reasoning still leave room for content.
    max_tokens = model_cap if os.environ.get("KIMI_USE_MAX_OUTPUT", "1") == "1" else requested
    # Hidden reasoning shares max_tokens. Kimi Code's 'max' UI level is not an
    # NVIDIA wire value and can exhaust the budget with long hidden thinking.
    if effort == "max":
        effort = os.environ.get("KIMI_MAX_EFFORT_ALIAS", "high")
    drop_effort = False
    if model_cfg.get("drop_none_effort") and effort == "none":
        drop_effort = True
    body = {
        **({} if drop_effort else {"reasoning_effort": effort}),
        "stream": True,
        "model": model_id,
        "seed": int(req.get("seed") or 0),
        "max_tokens": max_tokens,
        "messages": src,
        "stream_options": {"include_usage": True, "continuous_usage_stats": True},
    }
    for k in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "stop"):
        if req.get(k) is not None:
            body[k] = req[k]
    for k in ("tools", "tool_choice", "response_format", "parallel_tool_calls"):
        if req.get(k) is not None:
            body[k] = req[k]
    return fit_payload_budget(body)


def _sse_is_finished(buf):
    """Check for a real terminal OpenAI chunk, not just a closed HTTP body."""
    if "[DONE]" in buf[-400:]:
        return True
    for blk in buf.split("\n\n"):
        for ln in blk.splitlines():
            if not ln.startswith("data:"):
                continue
            try:
                obj = json.loads(ln[5:].strip())
            except (ValueError, TypeError):
                continue
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    return True
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("http " + fmt % args)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {
                "object": "list",
                "data": [{
                    "id": mid, "object": "model", "created": int(time.time()),
                    "owned_by": "nvidia playground bridge (%s)" % mid.split("/", 1)[0],
                    "context_length": cfg.get("context", 262144),
                } for mid, cfg in MODELS.items()],
            })
        if self.path.rstrip("/").endswith("/health"):
            return self._json(200, {"ok": True})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            return self._json(400, {"error": {"message": "invalid JSON"}})

        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})

        dump("req", json.dumps(req, ensure_ascii=False))

        model_id, cfg = resolve_model(req.get("model"))
        if cfg is None:
            return self._json(400, {"error": {
                "message": "unknown model %r; available: %s" % (req.get("model"), ", ".join(sorted(MODELS))),
                "type": "invalid_request_error",
            }})
        req["model"] = model_id
        want_stream = bool(req.get("stream"))
        waited = time.time()
        sem = _model_slots.get(model_id) or _slots
        sem_desc = "global limit %d" % CONCURRENCY
        sem.acquire()
        if time.time() - waited > 0.5:
            log("queued %.0fs waiting for a free upstream slot (%s)"
                % (time.time() - waited, sem_desc))
        try:
            self._serve(req, want_stream, model_id, cfg)
        except CdpError as e:
            log(f"upstream failure: {e}")
            self._fail(503, str(e))
        except BrokenPipeError:
            log("client disconnected")
        except Exception as e:  # noqa: BLE001
            log(f"error: {e!r}")
            self._fail(500, str(e))
        finally:
            sem.release()

    def _fail(self, status, message):
        """Report an error without corrupting an already-open SSE response."""
        if getattr(self, "_opened", False):
            try:
                self._chunk_write("data: " + json.dumps(
                    {"error": {"message": message, "type": "bridge_error"}}) + "\n\n")
                self._chunk_end()
            except Exception:
                pass
            return
        self._json(status, {"error": {"message": message, "type": "bridge_error"}})

    def _serve(self, req, want_stream, model_id=MODEL, cfg=None):
        cfg = cfg or MODELS[model_id]
        upstream = upstream_for(cfg)
        fnid = cfg["fnid"]
        page_url = cfg["page"]
        t0 = time.time()
        body = build_upstream_body(req, model_id, cfg)
        log(f"request model={model_id} msgs={len(body['messages'])} stream={want_stream} effort={body.get('reasoning_effort','none')} tools={len(body.get('tools') or [])}")
        if body.get("tools") and not _seen_tools[0]:
            _seen_tools[0] = True
            log("tool names: " + ", ".join((t.get("function") or {}).get("name", "?")
                                           for t in body["tools"]))
        pace()
        t_paced = time.time()
        _last_req[0] = t_paced

        with _lock:
            cdp = Cdp(page_url)
        t_cdp = time.time()
        t_mint = t_cdp
        try:
            token = take_cached_token(model_id)
            if not token:
                token = _mint_any(model_id, page_url)
            if not token:
                raise CdpError("hCaptcha mint failed (page may need a visible browser)")
            t_mint = time.time()

            stream = self._open(want_stream, req)
            ok = False
            last = "upstream rejected the request"
            for attempt in range(OVERLOAD_ATTEMPTS):
                self._captcha_error = False
                self._retryable = False
                self._stream_started = False
                self._tool_ids = {}
                self._norm_pending = ""
                if attempt:
                    # A failed attempt consumes the captcha token, so every
                    # retry needs a brand new one before hitting upstream again.
                    token = take_cached_token(model_id) or _mint_any(model_id, page_url)
                    if not token:
                        raise CdpError("hCaptcha mint failed (page may need a visible browser)")
                # unique per request: two requests landing in the same millisecond
                # would otherwise share a binding name and their streams would mix.
                binding = "kimiChunk" + uuid.uuid4().hex[:16]
                ok = self._relay(cdp, body, token, binding, stream, want_stream, upstream, fnid)
                if ok:
                    break
                if self._stream_started:
                    log("upstream failed mid-stream; cannot retry")
                    return
                if not want_stream:
                    stream["chunks"] = []
                if self._captcha_error:
                    last = "hCaptcha token rejected by NVIDIA"
                    with _cache_lock:
                        n = len(_pool)
                        _pool.clear()
                    log("captcha rejected; refreshing captcha context (%d cached tokens dropped)" % n)
                    # A rejection often means the page's captcha state went stale
                    # (silent challenge / expired token). Reloading gives a clean
                    # widget and a token that NVIDIA accepts.
                    if attempt >= 1:
                        cdp.reload_page()
                    time.sleep(1.0 + attempt)
                    continue
                if self._retryable:
                    last = "NVIDIA inference pool is saturated (HTTP 5xx), try again in a minute"
                    if attempt == OVERLOAD_ATTEMPTS - 1:
                        break
                    delay = (5, 15)[min(attempt, 1)]
                    log(f"upstream overloaded; retrying in {delay}s")
                    time.sleep(delay)
                    continue
                last = "upstream rejected the request"
                break
            if not ok:
                raise CdpError(last)
        finally:
            cdp.close()
            log("timing: pace %.0fms, cdp %.0fms, mint %.0fms, total %.0fms"
                % ((t_paced - t0) * 1000, (t_cdp - t_paced) * 1000,
                   (t_mint - t_cdp) * 1000, (time.time() - t0) * 1000))

    def _open(self, want_stream, req):
        if want_stream:
            self._opened = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            return None
        return {"chunks": []}

    def _relay(self, cdp, body, token, binding, stream, want_stream, upstream=UPSTREAM, fnid=FUNCTION_ID):
        cdp.call("Runtime.addBinding", name=binding)
        js = STREAM_JS % {
            "binding": json.dumps(binding),
            "url": json.dumps(upstream),
            "token": json.dumps(token),
            "fnid": json.dumps(fnid),
            "body": json.dumps(json.dumps(body)),
        }
        cdp.call("Runtime.evaluate", expression=js, returnByValue=False, awaitPromise=False)
        t_sent = time.time()
        deadline = time.time() + TOTAL_TIMEOUT
        idle_until = t_sent + IDLE_TIMEOUT
        buf = ""
        ok = True
        started = False
        logged_ttfb = False
        n_chunks = 0
        n_bytes = 0
        last_report = t_sent
        while time.time() < deadline:
            try:
                msg = json.loads(cdp.ws.recv())
            except websocket.WebSocketTimeoutException:
                if time.time() >= idle_until:
                    log("upstream stalled: no data for %.0fs; aborting" % IDLE_TIMEOUT)
                    self._retryable = not started
                    if want_stream and started:
                        self._chunk_write("data: " + json.dumps(
                            {"error": {"message": "upstream stalled (no data for %ds)" % IDLE_TIMEOUT,
                                       "type": "upstream_timeout"}}) + "\n\n")
                        self._chunk_end()
                    return False
                continue
            if msg.get("method") != "Runtime.bindingCalled":
                continue
            p = msg.get("params", {})
            if p.get("name") != binding:
                continue
            payload = p.get("payload") or ""
            if payload and not payload.startswith("__"):
                idle_until = time.time() + IDLE_TIMEOUT
                n_chunks += 1
                n_bytes += len(payload)
                if time.time() - last_report > 30:
                    log("progress %.0fs: %d chunks, %.1f MB"
                        % (time.time() - t_sent, n_chunks, n_bytes / 1e6))
                    last_report = time.time()
                if n_bytes > MAX_STREAM_BYTES:
                    log("aborting runaway response: %.1f MB" % (n_bytes / 1e6))
                    if want_stream and started:
                        self._chunk_write("data: " + json.dumps(
                            {"error": {"message": "response exceeded %.0f MB, aborted"
                             % (MAX_STREAM_BYTES / 1_000_000),
                                       "type": "runaway_response"}}) + "\n\n")
                        self._chunk_end()
                    return False
            if not logged_ttfb and payload and not payload.startswith("__"):
                log("ttfb %.0fms after dispatch" % ((time.time() - t_sent) * 1000))
                logged_ttfb = True
            if payload == "__END__":
                # A closed upstream socket is not proof of a complete turn.
                # Some NVIDIA responses end without a final chunk; report that
                # honestly instead of letting DSH only say "no finish_reason".
                if not buf:
                    self._retryable = not started
                    log("NVIDIA closed the stream before any response")
                elif not _sse_is_finished(buf):
                    log("upstream ended without finish_reason")
                    if not started:
                        self._retryable = True
                        return False
                    if want_stream:
                        # Do not invent a normal finish_reason for an unfinished
                        # turn. DSH must surface the truncation, not a fake success.
                        err = {"error": {"message": "NVIDIA ended the response before finish_reason", "type": "incomplete_upstream_stream"}}
                        self._chunk_write("data: " + json.dumps(err) + "\n\n")
                        self._chunk_end()
                    return False
                break
            if payload.startswith("__ERR__"):
                detail = payload[7:]
                log(f"upstream error: {detail[:200]}")
                if detail.startswith("400") and ("Captcha" in detail or "Token is invalid" in detail):
                    self._captcha_error = True
                    return False if not started else True
                if detail.startswith(("429", "500", "502", "503", "504")) or "Pool#acquire" in detail:
                    self._retryable = True
                if want_stream and started:
                    err = {"error": {"message": detail[:300], "type": "upstream_error"}}
                    self._chunk_write("data: " + json.dumps(err) + "\n\n")
                    self._chunk_end()
                return False
            buf += payload
            if want_stream:
                started = True
                self._stream_started = True
                payload = self._normalize_tool_stream(payload)
                if payload:
                    self._chunk_write(payload)
            else:
                stream["chunks"].append(payload)
        else:
            # The loop ran out of time without __END__ (no break). Ending the
            # body silently made clients report "stream closed before
            # response.completed"; say what happened instead.
            log("upstream exceeded total timeout of %.0fs; aborting" % TOTAL_TIMEOUT)
            self._retryable = False
            if want_stream and started:
                self._chunk_write("data: " + json.dumps(
                    {"error": {"message": "response exceeded the bridge total timeout (%ds)" % TOTAL_TIMEOUT,
                               "type": "upstream_timeout"}}) + "\n\n")
                self._chunk_end()
            return False

        if want_stream:
            dump("stream", buf)
            self._chunk_end()
            return ok

        dump("stream", buf)
        text = "".join(stream["chunks"])
        self._json(200, self._merge_sse(text, body))
        return True
    def _chunk_write(self, text):
        data = text.encode()
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _chunk_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _normalize_tool_stream(self, payload):
        """Split packed tool-call deltas so UIs render tool bubbles.

        NVIDIA sends one delta carrying id `name:idx` + name + full arguments.
        We emit, per tool call:
          1) a name-announcement delta (call_ id + name, empty args)
          2) an arguments-only delta (no repeated id/name)
        SSE lines may arrive fragmented across binding payloads, so we buffer
        partial lines in self._norm_pending and only rewrite complete
        `data: {...}\\n\\n` blocks. Non-tool lines pass through untouched.
        """
        data = getattr(self, "_norm_pending", "") + payload
        blocks = data.split("\n\n")
        self._norm_pending = blocks.pop()  # last piece may be incomplete
        out = []
        for blk in blocks:
            line = blk if blk.startswith("data:") else "data: " + blk
            body = line[5:].strip()
            if not body or body == "[DONE]":
                out.append(blk + "\n\n")
                continue
            try:
                obj = json.loads(body)
            except Exception:
                out.append(blk + "\n\n")
                continue
            choices = obj.get("choices") or []
            if not any((ch.get("delta") or {}).get("tool_calls") for ch in choices):
                out.append(blk + "\n\n")
                continue
            finish_reason = None
            frags = []
            for ch in choices:
                d = ch.get("delta") or {}
                tcs = d.get("tool_calls") or []
                if not tcs:
                    continue
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in tcs:
                    idx = tc.get("index", 0)
                    fn = tc.get("function") or {}
                    cid = self._tool_ids.get(idx)
                    if not cid:
                        cid = "call_" + uuid.uuid4().hex[:24]
                        self._tool_ids[idx] = cid
                    # (1) announce: call_ id + name, empty args -> renders bubble
                    announce = dict(obj)
                    announce["choices"] = [dict(ch, delta=dict(d, tool_calls=[{
                        "index": idx, "id": cid, "type": "function",
                        "function": {"name": fn.get("name", ""), "arguments": ""}}]),
                        finish_reason=None)]
                    frags.append(announce)
                    # (2) arguments fragment only: no repeated id/name
                    full = dict(obj)
                    full["choices"] = [dict(ch, delta=dict(d, tool_calls=[{
                        "index": idx,
                        "function": {"arguments": fn.get("arguments", "")}}]),
                        finish_reason=None)]
                    frags.append(full)
            # NVIDIA sometimes packs finish_reason into the final tool delta.
            # Emitting it on the first split fragment makes the next fragment
            # look like "content after the finish reason" to strict clients.
            if finish_reason and frags:
                frags[-1]["choices"][0]["finish_reason"] = finish_reason
            for frag in frags:
                out.append("data: " + json.dumps(frag) + "\n\n")
        return "".join(out)



    def _chunk_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    @staticmethod
    def _merge_sse(text, body):
        content, reasoning, usage, rid, created, finish = [], [], None, None, None, None
        tools = {}
        model = body.get("model") or MODEL
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            rid = obj.get("id") or rid
            created = obj.get("created") or created
            model = obj.get("model") or model
            usage = obj.get("usage") or usage
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tools.setdefault(idx, {"id": "", "type": "function",
                                                  "function": {"name": "", "arguments": ""}})
                    if not slot["id"]:
                        slot["id"] = "call_" + uuid.uuid4().hex[:24]
                    fn = tc.get("function") or {}
                    if fn.get("name") and not slot["function"]["name"]:
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        msg = {"role": "assistant", "content": "".join(content) or None}
        if reasoning:
            msg["reasoning_content"] = "".join(reasoning)
        if tools:
            msg["tool_calls"] = [tools[i] for i in sorted(tools)]
            if msg["content"] is None:
                msg["content"] = None
        out = {
            "id": rid or "chatcmpl-bridge",
            "object": "chat.completion",
            "created": created or int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish or "stop"}],
        }
        if usage:
            out["usage"] = usage
        return out


def main():
    threading.Thread(target=_premint_loop, name="premint", daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    log(f"listening on http://127.0.0.1:{PORT}/v1 (default model {MODEL}; available: {', '.join(sorted(MODELS))})")
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
