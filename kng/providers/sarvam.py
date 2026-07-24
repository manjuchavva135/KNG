"""Low-level Sarvam access shared by the LLM/ASR/OCR/translate providers.

Uses the official `sarvamai` SDK for chat / speech-to-text / translate, and a
direct REST call for the Document Intelligence (OCR) job API, which the SDK does
not yet expose. All auth uses the `api-subscription-key` header.
"""
from __future__ import annotations

from functools import lru_cache

from ..config import settings

SARVAM_BASE = "https://api.sarvam.ai"


@lru_cache(maxsize=1)
def client():
    """Cached sarvamai SDK client."""
    from sarvamai import SarvamAI
    s = settings()
    if not s.sarvam_api_key:
        raise RuntimeError("SARVAM_API_KEY not set in .env")
    return SarvamAI(api_subscription_key=s.sarvam_api_key)


def _headers() -> dict:
    return {"api-subscription-key": settings().sarvam_api_key}


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
