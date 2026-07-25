"""Normalization: per-segment language detection + optional English translation.

Language is detected by Unicode script (robust for short, code-switched news
text where statistical detectors wobble): Telugu, Devanagari (Hindi), Latin.
"""
from __future__ import annotations

from ..models import ExtractedDoc

_TELUGU = range(0x0C00, 0x0C80)
_DEVANAGARI = range(0x0900, 0x0980)


def detect_language(text: str) -> str:
    te = hi = en = 0
    for ch in text:
        o = ord(ch)
        if o in _TELUGU:
            te += 1
        elif o in _DEVANAGARI:
            hi += 1
        elif ch.isascii() and ch.isalpha():
            en += 1
    total = te + hi + en
    if total == 0:
        return "unknown"
    scores = {"te": te / total, "hi": hi / total, "en": en / total}
    top, frac = max(scores.items(), key=lambda kv: kv[1])
    # if a second script is also substantial → mixed (very common here)
    second = sorted(scores.values(), reverse=True)[1]
    if second >= 0.25:
        return "mixed"
    return top if frac >= 0.5 else "mixed"


def normalize_doc(doc: ExtractedDoc, translate: bool = False) -> ExtractedDoc:
    translator = None
    if translate:
        from ..providers import get_translator
        translator = get_translator()
    for seg in doc.segments:
        seg.language = detect_language(seg.text_original)
        if translator is not None and seg.language in {"te", "hi", "mixed"} and seg.text_original:
            try:
                seg.text_en = translator.to_english(seg.text_original)
                if seg.text_en:
                    doc.sarvam_calls["translate"] = doc.sarvam_calls.get("translate", 0) + 1
            except Exception:
                seg.text_en = None
    return doc
