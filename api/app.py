"""
hCaptcha browser solver — opens a local demo page, runs the widget, intercepts
api.hcaptcha.com/getcaptcha/{sitekey}, returns token + raw API body.

Default sitekey matches nw.php / hcaptcha_lib.php.

Ubuntu server:
  pip install -r requirements.txt
  playwright install --with-deps chromium
  HEADLESS=1 python api.py
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# nw.php initializeHCaptcha() sitekey
DEFAULT_SITEKEY = "12baaa15-55cb-409a-bbbe-900132d52afa"
DEFAULT_HOST = "www.epassport.gov.bd"
DEFAULT_PAGE_URL = "https://www.epassport.gov.bd/"
GETCAPTCHA_BASE = "https://api.hcaptcha.com/getcaptcha/"
TOKEN_TTL = 110

_IS_LINUX = platform.system() == "Linux"
# Headless by default on Linux servers (no DISPLAY) unless HEADLESS=0
_DEFAULT_HEADLESS = _IS_LINUX and not os.environ.get("DISPLAY")


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


DEFAULT_HEADLESS = _env_bool("HEADLESS", _DEFAULT_HEADLESS)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    if _IS_LINUX
    else (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
)

# Required on Ubuntu VPS / Docker (sandbox + small /dev/shm)
_LINUX_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
]

_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_BROWSER_LOCK = threading.Lock()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _demo_html(sitekey: str, host: str) -> str:
    sk = json.dumps(sitekey)
    h = json.dumps(host)
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>hCaptcha demo</title>
<script src="https://js.hcaptcha.com/1/api.js?render=explicit" async defer></script>
<style>body{{font-family:system-ui;margin:24px;background:#f4f4f8}}
#hcaptcha-container{{min-height:78px}}</style>
</head><body>
<h3>hCaptcha demo</h3>
<div id="hcaptcha-container"></div>
<pre id="out" style="font-size:11px;word-break:break-all"></pre>
<script>
const SITEKEY = {sk};
const HOST = {h};
let widgetId = null;
window.__token = null;
window.__getcaptcha = null;

function setOut(msg) {{
  const el = document.getElementById('out');
  if (el) el.textContent = typeof msg === 'string' ? msg : JSON.stringify(msg, null, 2);
}}

function onCaptchaSuccess(token) {{
  window.__token = token;
  setOut({{ source: 'hcaptcha_callback', token_preview: token.slice(0, 80) + '...' }});
}}

function onCaptchaExpired() {{
  setTimeout(() => {{
    if (widgetId !== null && typeof hcaptcha !== 'undefined') hcaptcha.execute(widgetId);
  }}, 1500);
}}

function onCaptchaError(err) {{
  window.__error = String(err);
  setOut({{ error: window.__error }});
}}

function initWidget() {{
  if (typeof hcaptcha === 'undefined' || widgetId) return;
  const opts = {{
    sitekey: SITEKEY,
    callback: onCaptchaSuccess,
    'expired-callback': onCaptchaExpired,
    'error-callback': onCaptchaError
  }};
  if (HOST) opts.host = HOST;
  widgetId = hcaptcha.render('hcaptcha-container', opts);
  window.__widgetReady = true;
}}

window.addEventListener('load', () => {{
  const tick = setInterval(() => {{
    if (typeof hcaptcha !== 'undefined') {{
      clearInterval(tick);
      initWidget();
    }}
  }}, 80);
}});

const GETCAPTCHA_MATCH = 'api.hcaptcha.com/getcaptcha/';

function captureGetCaptcha(bodyText, url) {{
  window.__getcaptcha = {{ url, body: bodyText, at: Date.now() }};
  try {{
    window.__getcaptcha.json = JSON.parse(bodyText);
  }} catch (e) {{}}
}}

(function installInterceptors() {{
  const origFetch = window.fetch;
  window.fetch = async function(...args) {{
    const res = await origFetch.apply(this, args);
    try {{
      const req = args[0];
      const url = typeof req === 'string' ? req : (req?.url || '');
      if (url.includes(GETCAPTCHA_MATCH)) {{
        const clone = res.clone();
        captureGetCaptcha(await clone.text(), url);
      }}
    }} catch (e) {{}}
    return res;
  }};
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
    this._interceptUrl = url;
    return origOpen.call(this, method, url, ...rest);
  }};
  XMLHttpRequest.prototype.send = function(...args) {{
    this.addEventListener('load', function() {{
      const url = this._interceptUrl || '';
      if (url.includes(GETCAPTCHA_MATCH)) {{
        captureGetCaptcha(this.responseText, url);
      }}
    }});
    return origSend.apply(this, args);
  }};
}})();

window.runSolve = function() {{
  return new Promise((resolve, reject) => {{
    if (widgetId === null || typeof hcaptcha === 'undefined') {{
      reject(new Error('hcaptcha not ready'));
      return;
    }}
    hcaptcha.reset(widgetId);
    setTimeout(() => {{
      try {{ hcaptcha.execute(widgetId); resolve(true); }}
      catch (e) {{ reject(e); }}
    }}, 400);
  }});
}};
</script></body></html>"""


def _extract_token(body: str) -> str | None:
    if not body:
        return None
    text = body.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if len(text) > 80 and not text.startswith("<"):
            return text
        return None
    for key in ("generated_pass_UUID", "pass", "key", "token", "response"):
        val = data.get(key)
        if isinstance(val, str) and len(val) > 80:
            return val
    c = data.get("c")
    if isinstance(c, dict):
        t = c.get("token")
        if isinstance(t, str) and len(t) > 80:
            return t
    return None


def _cache_get(sitekey: str) -> dict[str, Any] | None:
    entry = _TOKEN_CACHE.get(sitekey)
    if not entry:
        return None
    if time.time() >= entry.get("expires_at", 0):
        _TOKEN_CACHE.pop(sitekey, None)
        return None
    return entry


def _cache_put(sitekey: str, payload: dict[str, Any]) -> None:
    payload["expires_at"] = time.time() + TOKEN_TTL
    payload["cached_at"] = time.time()
    _TOKEN_CACHE[sitekey] = payload


@dataclass
class SolveResult:
    success: bool
    sitekey: str
    token: str | None = None
    source: str | None = None
    getcaptcha_url: str = ""
    getcaptcha_body: dict[str, Any] | str | None = None
    getcaptcha_raw: str | None = None
    error: str | None = None
    took_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "sitekey": self.sitekey,
            "token": self.token,
            "source": self.source,
            "getcaptcha_url": self.getcaptcha_url or (GETCAPTCHA_BASE + self.sitekey),
            "getcaptcha": self.getcaptcha_body,
            "getcaptcha_raw_preview": (
                (self.getcaptcha_raw or "")[:500] if self.getcaptcha_raw else None
            ),
            "error": self.error,
            "took_ms": round(self.took_ms, 1),
            **self.extra,
        }


class _DemoHandler(BaseHTTPRequestHandler):
    html: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        body = self.html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_demo_server(sitekey: str, host: str) -> tuple[HTTPServer, str]:
    port = _free_port()
    handler = type("H", (_DemoHandler,), {"html": _demo_html(sitekey, host)})
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/"


def _browser_launch_args() -> list[str]:
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--mute-audio",
    ]
    if _IS_LINUX or _env_bool("CHROMIUM_NO_SANDBOX", False):
        args.extend(_LINUX_BROWSER_ARGS)
    return args


def solve_hcaptcha(
    sitekey: str = DEFAULT_SITEKEY,
    *,
    host: str = DEFAULT_HOST,
    page_url: str = DEFAULT_PAGE_URL,
    headless: bool | None = None,
    timeout_sec: float = 90.0,
    force: bool = False,
    show_browser: bool | None = None,
) -> SolveResult:
    """
    Launch Chromium, open local demo page, execute widget, capture getcaptcha + token.
    On Ubuntu servers, runs headless with --no-sandbox / --disable-dev-shm-usage.
    """
    if headless is None:
        headless = DEFAULT_HEADLESS

    if not force:
        cached = _cache_get(sitekey)
        if cached and cached.get("token"):
            return SolveResult(
                success=True,
                sitekey=sitekey,
                token=cached["token"],
                source="cache",
                getcaptcha_url=cached.get("getcaptcha_url", GETCAPTCHA_BASE + sitekey),
                getcaptcha_body=cached.get("getcaptcha_body"),
                took_ms=0.0,
                extra={"from_cache": True},
            )

    t0 = time.perf_counter()
    getcaptcha_url = GETCAPTCHA_BASE + sitekey
    captured: dict[str, Any] = {"body": None, "raw": None, "url": None}
    token_holder: dict[str, str | None] = {"token": None, "source": None}

    if show_browser is None:
        show_browser = not headless

    use_headless = bool(headless and not show_browser)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return SolveResult(
            success=False,
            sitekey=sitekey,
            error=(
                "playwright not installed — "
                "pip install playwright && playwright install --with-deps chromium"
            ),
            took_ms=(time.perf_counter() - t0) * 1000,
        )

    server, demo_url = _start_demo_server(sitekey, host)

    def on_response(response: Any) -> None:
        url = response.url
        if "api.hcaptcha.com/getcaptcha/" not in url or sitekey not in url:
            return
        try:
            raw = response.text()
        except Exception:
            return
        captured["url"] = url
        captured["raw"] = raw
        try:
            captured["body"] = json.loads(raw)
        except json.JSONDecodeError:
            captured["body"] = raw
        tok = _extract_token(raw)
        if tok:
            token_holder["token"] = tok
            token_holder["source"] = "getcaptcha_api"

    page_err: str | None = None
    try:
        with _BROWSER_LOCK:
            with sync_playwright() as p:
                launch_opts: dict[str, Any] = {
                    "headless": use_headless,
                    "args": _browser_launch_args(),
                    "ignore_default_args": ["--enable-automation"],
                }
                # Prefer Playwright-bundled Chromium on Linux; Chrome channel is optional
                browser = None
                prefer_chrome = _env_bool("USE_SYSTEM_CHROME", not _IS_LINUX)
                if prefer_chrome:
                    try:
                        browser = p.chromium.launch(channel="chrome", **launch_opts)
                    except Exception:
                        browser = None
                if browser is None:
                    browser = p.chromium.launch(**launch_opts)
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    timezone_id="Asia/Dhaka",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
                )
                page = context.new_page()
                page.on("response", on_response)
                page.goto(demo_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_function(
                    "() => window.__widgetReady === true",
                    timeout=45000,
                )
                page.evaluate("window.runSolve()")

                deadline = time.time() + timeout_sec
                while time.time() < deadline:
                    cb_token = page.evaluate("window.__token")
                    if isinstance(cb_token, str) and len(cb_token) > 80:
                        token_holder["token"] = cb_token
                        token_holder["source"] = token_holder["source"] or "hcaptcha_callback"
                    if token_holder["token"]:
                        break
                    err = page.evaluate("window.__error")
                    if isinstance(err, str) and err:
                        page_err = err
                    time.sleep(0.25)

                gc = page.evaluate("window.__getcaptcha")
                if isinstance(gc, dict):
                    if not captured.get("url") and gc.get("url"):
                        captured["url"] = gc["url"]
                    body_text = gc.get("body")
                    if body_text and not captured.get("raw"):
                        captured["raw"] = body_text
                        try:
                            captured["body"] = gc.get("json") or json.loads(body_text)
                        except json.JSONDecodeError:
                            captured["body"] = body_text
                        tok = _extract_token(str(body_text))
                        if tok and not token_holder["token"]:
                            token_holder["token"] = tok
                            token_holder["source"] = "getcaptcha_api"

                browser.close()
    except Exception as e:
        return SolveResult(
            success=False,
            sitekey=sitekey,
            error=str(e),
            getcaptcha_url=getcaptcha_url,
            getcaptcha_body=captured.get("body"),
            getcaptcha_raw=captured.get("raw"),
            took_ms=(time.perf_counter() - t0) * 1000,
        )
    finally:
        server.shutdown()

    took = (time.perf_counter() - t0) * 1000
    token = token_holder["token"]
    if not token and captured.get("raw"):
        token = _extract_token(str(captured["raw"]))

    if not token:
        return SolveResult(
            success=False,
            sitekey=sitekey,
            error=page_err or "timeout — no token (HSW/image challenge may need visible browser)",
            getcaptcha_url=captured.get("url") or getcaptcha_url,
            getcaptcha_body=captured.get("body"),
            getcaptcha_raw=captured.get("raw"),
            took_ms=took,
        )

    result = SolveResult(
        success=True,
        sitekey=sitekey,
        token=token,
        source=token_holder["source"],
        getcaptcha_url=captured.get("url") or getcaptcha_url,
        getcaptcha_body=captured.get("body"),
        getcaptcha_raw=captured.get("raw"),
        took_ms=took,
    )
    _cache_put(
        sitekey,
        {
            "token": token,
            "source": result.source,
            "getcaptcha_url": result.getcaptcha_url,
            "getcaptcha_body": result.getcaptcha_body,
        },
    )
    return result


def get_cached_token(sitekey: str = DEFAULT_SITEKEY) -> str | None:
    entry = _cache_get(sitekey)
    return entry.get("token") if entry else None


def clear_cache(sitekey: str | None = None) -> None:
    if sitekey:
        _TOKEN_CACHE.pop(sitekey, None)
    else:
        _TOKEN_CACHE.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="Solve hCaptcha via browser demo page")
    parser.add_argument("--sitekey", default=DEFAULT_SITEKEY)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_HEADLESS,
        help=f"Run Chromium headless (default: {DEFAULT_HEADLESS})",
    )
    parser.add_argument("--force", action="store_true", help="Ignore token cache")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    print(f"Sitekey: {args.sitekey}")
    print(f"getcaptcha: {GETCAPTCHA_BASE}{args.sitekey}")
    print(f"headless={args.headless} platform={platform.system()}")
    print("Opening demo page in browser…")

    result = solve_hcaptcha(
        args.sitekey,
        host=args.host,
        headless=args.headless,
        force=args.force,
        timeout_sec=args.timeout,
    )
    out = result.to_dict()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if result.success and result.token:
        print("\n--- TOKEN ---")
        print(result.token)
    if result.getcaptcha_body:
        print("\n--- GETCAPTCHA (parsed) ---")
        print(
            json.dumps(result.getcaptcha_body, indent=2, ensure_ascii=False)
            if isinstance(result.getcaptcha_body, dict)
            else result.getcaptcha_body
        )


if __name__ == "__main__":
    main()
