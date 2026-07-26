"""Low-level Sarvam access shared by the LLM/ASR/OCR/translate providers.

Uses the official `sarvamai` SDK for chat / speech-to-text / translate, and a
direct REST call for the Document Intelligence (OCR) job API, which the SDK does
not yet expose. All auth uses the `api-subscription-key` header.
"""
from __future__ import annotations

import json
import threading
import time
from functools import lru_cache

from ..config import settings

SARVAM_BASE = "https://api.sarvam.ai"


@lru_cache(maxsize=1)
def client():
    """Cached sarvamai SDK client.

    The timeout is set here, on the client, rather than left to per-request
    `request_options`. A graph run at concurrency 16 had all 16 workers block on
    responses that never arrived and never timed out — the process sat at 0.3%
    CPU for 35 minutes holding 16 open connections. A client-level timeout
    configures the underlying httpx client directly and is the backstop that
    actually fires, so a stalled call fails and retries instead of hanging.

    The SDK's own default is 60s, which is too short for the chat models used
    for graph extraction; this raises it rather than lowering it.
    """
    from sarvamai import SarvamAI
    s = settings()
    if not s.sarvam_api_key:
        raise RuntimeError("SARVAM_API_KEY not set in .env")
    return SarvamAI(api_subscription_key=s.sarvam_api_key, timeout=s.llm_timeout)


def _headers() -> dict:
    return {"api-subscription-key": settings().sarvam_api_key}


@lru_cache(maxsize=1)
def _http():
    """Shared httpx client for direct REST chat calls.

    The SDK is bypassed for chat for the same reason it already is for Document
    Intelligence: control. Two graph runs hung with every worker blocked on
    responses that never returned and never timed out — once at concurrency 16
    and again at 4 — while a direct httpx POST to the same endpoint answered in
    one second. Setting the timeout on the SDK client did not fix it. An
    explicit `httpx.Timeout` here does fire, so a stalled call fails and retries
    instead of parking a worker forever.
    """
    import httpx
    s = settings()
    return httpx.Client(
        timeout=httpx.Timeout(s.llm_timeout, connect=15.0, pool=30.0),
        # **Keep-alive is disabled deliberately.** The server drops idle
        # connections without the close reaching us, so a pooled socket stays
        # ESTABLISHED locally while being dead at the far end; the next request
        # on it is never answered and the worker blocks until the read timeout,
        # burning `llm_timeout x (retries+1)` per chunk. Every long graph run
        # died this way — all workers in `poll_schedule_timeout` on live-looking
        # sockets, while a fresh connection from another process answered in one
        # second. Rate limiting made it worse by adding the idle gaps that let
        # connections go stale. A new connection per request costs a TLS
        # handshake (~100ms) against calls that take 15-40s: irrelevant.
        limits=httpx.Limits(max_connections=32, max_keepalive_connections=0),
        headers={**_headers(), "Connection": "close"},
    )


class _RateLimiter:
    """Token bucket over a sliding minute, shared by every worker thread.

    Sarvam publishes 40 req/min for the large chat models on the Starter tier,
    counted **per account** rather than per key. Staying under that by design is
    better than discovering it: exceeding it earlier produced connections that
    hung rather than clean 429s, which cost two runs and ~45 minutes of wall
    clock before the cause was clear.
    """

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self._times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 60.0]
                if len(self._times) < self.per_minute:
                    self._times.append(now)
                    return
                wait = 60.0 - (now - self._times[0]) + 0.05
            time.sleep(max(0.05, wait))


@lru_cache(maxsize=1)
def _limiter() -> _RateLimiter:
    return _RateLimiter(settings().llm_rpm)


def chat_completion(payload: dict) -> dict:
    """POST /v1/chat/completions and return the parsed body.

    Raises on a non-2xx so the caller's retry/backoff sees a real error — and so
    a permanent 4xx (a deprecated model, an over-tier `max_tokens`) surfaces
    immediately rather than being retried.

    `KNG_LLM_TRACE=1` prints one line per request: how long the rate limiter
    held it, how long the HTTP call took, and the status. A long graph pass is
    otherwise a black box between `[n/total]` lines — during the 2026-07-25 run
    it was impossible to tell "throttled by our own limiter" from "waiting on a
    slow response" from outside the process, which cost two needless restarts.
    """
    import os
    import sys

    import httpx
    trace = os.environ.get("KNG_LLM_TRACE", "").lower() in {"1", "true", "yes", "on"}
    t0 = time.monotonic()
    _limiter().acquire()
    t1 = time.monotonic()
    try:
        r = _http().post(f"{SARVAM_BASE}/v1/chat/completions", json=payload)
    except Exception as e:
        if trace:
            print(f"[llm] wait={t1 - t0:5.1f}s http={time.monotonic() - t1:6.1f}s "
                  f"EXC {type(e).__name__}: {str(e)[:120]}", file=sys.stderr, flush=True)
        raise
    if trace:
        print(f"[llm] wait={t1 - t0:5.1f}s http={time.monotonic() - t1:6.1f}s "
              f"status={r.status_code} bytes={len(r.content)}", file=sys.stderr, flush=True)
    if r.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"{r.status_code}: {r.text[:400]}", request=r.request, response=r)
    return r.json()


def chat_completion_stream(payload: dict):
    """POST /v1/chat/completions with `stream: true`, yielding content deltas.

    WP5's chat UI needs the answer to appear as it is written: synthesis takes
    10-30 s, and a blank page for that long reads as broken. Retrieval evidence
    can be shown almost immediately, but the prose has to stream.

    Goes through the same rate limiter and the same keep-alive-disabled client as
    `chat_completion` — both of those were paid for in dead runs (see `_http`)
    and apply identically here. Yields only assistant text; reasoning deltas and
    the `[DONE]` sentinel are skipped.
    """
    import os
    import sys

    import httpx
    trace = os.environ.get("KNG_LLM_TRACE", "").lower() in {"1", "true", "yes", "on"}
    t0 = time.monotonic()
    _limiter().acquire()
    t1 = time.monotonic()
    chars = 0
    with _http().stream("POST", f"{SARVAM_BASE}/v1/chat/completions",
                        json={**payload, "stream": True}) as r:
        if r.status_code >= 400:
            body = r.read().decode("utf-8", "replace")[:400]
            raise httpx.HTTPStatusError(f"{r.status_code}: {body}",
                                        request=r.request, response=r)
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except ValueError:              # a keep-alive or partial frame
                continue
            for choice in event.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    chars += len(piece)
                    yield piece
    if trace:
        print(f"[llm] wait={t1 - t0:5.1f}s stream={time.monotonic() - t1:6.1f}s "
              f"chars={chars}", file=sys.stderr, flush=True)


def _unwrap(resp, *names: str) -> str:
    """SDK responses vary (object vs dict). Pull the first present field."""
    for n in names:
        if isinstance(resp, dict) and resp.get(n):
            return str(resp[n])
        val = getattr(resp, n, None)
        if val:
            return str(val)
    # chat-style: choices[0].message.content
    choices = getattr(resp, "choices", None) or (resp.get("choices") if isinstance(resp, dict) else None)
    if choices:
        c0 = choices[0]
        msg = getattr(c0, "message", None) or (c0.get("message") if isinstance(c0, dict) else None)
        if msg is not None:
            return str(getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else ""))
    return str(resp)
