"""Translation to English (secondary field for cross-lingual retrieval)."""
from __future__ import annotations

from .sarvam import _unwrap, client

# Sarvam translate REST limit ~1000-2000 chars → chunk long inputs.
_MAX = 900


class SarvamTranslator:
    def __init__(self, model: str):
        self.model = model

    def to_english(self, text: str, src: str = "auto") -> str:
        text = (text or "").strip()
        if not text:
            return ""
        out: list[str] = []
        for piece in _split(text, _MAX):
            resp = client().text.translate(
                input=piece, source_language_code=src,
                target_language_code="en-IN", model=self.model,
            )
            out.append(_unwrap(resp, "translated_text", "output", "text"))
        return " ".join(out).strip()


class NoTranslator:
    def to_english(self, text: str, src: str = "auto") -> str:
        return ""


def _split(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, cur = [], ""
    for sent in text.replace("\n", " ").split(". "):
        if len(cur) + len(sent) + 2 > size and cur:
            parts.append(cur)
            cur = ""
        cur += sent + ". "
    if cur.strip():
        parts.append(cur)
    return parts
