"""
FastAPI hCaptcha token service (Ubuntu-ready).

  GET /solve?sitekey=12baaa15-55cb-409a-bbbe-900132d52afa
  GET /?sitekey=...&format=json|raw
  GET /status?sitekey=...
  GET /clear?sitekey=...

Run (Ubuntu):
  HEADLESS=1 python api.py
  uvicorn api:app --host 0.0.0.0 --port 5000
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
import uvicorn

from solver import (
    DEFAULT_HEADLESS,
    DEFAULT_SITEKEY,
    GETCAPTCHA_BASE,
    SolveResult,
    clear_cache,
    get_cached_token,
    solve_hcaptcha,
)

SITEKEY_RE = re.compile(r"^[a-f0-9-]{36}$", re.I)
_solve_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1)

app = FastAPI(
    title="hCaptcha Solver API",
    version="1.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _validate_sitekey(sitekey: str) -> str:
    if not SITEKEY_RE.match(sitekey):
        raise HTTPException(400, detail={"success": False, "error": "invalid sitekey", "sitekey": sitekey})
    return sitekey


def _solve_sync(sitekey: str, headless: bool, timeout: float) -> SolveResult:
    with _solve_lock:
        return solve_hcaptcha(sitekey, headless=headless, force=True, timeout_sec=timeout)


async def _solve(sitekey: str, headless: bool, timeout: float) -> SolveResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _solve_sync, sitekey, headless, timeout)


def _cache_payload(sitekey: str, token: str) -> dict[str, Any]:
    return {
        "success": True,
        "from_cache": True,
        "sitekey": sitekey,
        "token": token,
        "getcaptcha_url": GETCAPTCHA_BASE + sitekey,
        "took_ms": 0,
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "headless_default": DEFAULT_HEADLESS}


@app.get("/status")
@app.get("/token")
async def status(sitekey: str = Query(default=DEFAULT_SITEKEY)) -> JSONResponse:
    sitekey = _validate_sitekey(sitekey.strip())
    token = get_cached_token(sitekey)
    body = {
        "success": bool(token),
        "valid": bool(token),
        "sitekey": sitekey,
        "token": token,
        "getcaptcha_url": GETCAPTCHA_BASE + sitekey,
    }
    return JSONResponse(body, status_code=200 if token else 404)


@app.get("/clear")
@app.get("/cache/clear")
async def clear(sitekey: str | None = Query(default=None)) -> dict[str, Any]:
    if sitekey:
        sitekey = _validate_sitekey(sitekey.strip())
    clear_cache(sitekey)
    return {"success": True, "cleared": True, "sitekey": sitekey or "all"}


@app.get("/solve", tags=["solve"])
@app.get("/", tags=["solve"], include_in_schema=False)
async def solve(
    sitekey: str = Query(default=DEFAULT_SITEKEY),
    format: Literal["json", "raw"] = Query(default="json", alias="format"),
    force: bool = Query(default=False),
    headless: bool = Query(default=DEFAULT_HEADLESS),
    timeout: float = Query(default=90.0, ge=10, le=180),
) -> Any:
    sitekey = _validate_sitekey(sitekey.strip())

    if not force:
        cached = get_cached_token(sitekey)
        if cached:
            if format == "raw":
                return PlainTextResponse(cached)
            return _cache_payload(sitekey, cached)

    result = await _solve(sitekey, headless, timeout)

    if format == "raw":
        if result.success and result.token:
            return PlainTextResponse(result.token)
        raise HTTPException(502, detail=result.error or "solve_failed")

    payload = result.to_dict()
    if not result.success:
        return JSONResponse(payload, status_code=502)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"Docs:  http://{args.host}:{args.port}/docs")
    print(f"Solve: http://{args.host}:{args.port}/solve?sitekey={DEFAULT_SITEKEY}")
    print(f"Raw:   http://{args.host}:{args.port}/solve?sitekey={DEFAULT_SITEKEY}&format=raw")
    print(f"headless_default={DEFAULT_HEADLESS}")

    uvicorn.run(
        "api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
