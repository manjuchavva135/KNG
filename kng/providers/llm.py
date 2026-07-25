"""LLM providers for entity/relation extraction and grounded synopsis.

Sarvam (sarvam-m / sarvam-30b / sarvam-105b) is primary; Anthropic optional.
"""
from __future__ import annotations

from .sarvam import _unwrap, client

# Strict cleanup prompt: reformat, never summarise. Used to route locally-parsed
# office-doc text through the Sarvam LLM so *every* document goes via the API
# (WP1b requirement) while preserving all content in its original language.
_CLEAN_SYSTEM = (
    "You are a document-cleanup engine. You are given raw text mechanically "
    "extracted from an office document (docx/pptx/xlsx). Reformat it into clean, "
    "well-structured Markdown.\n"
    "Rules:\n"
    "1. Preserve ALL content — every fact, name, number, date, quote and line.\n"
    "2. Do NOT summarise, translate, add, reorder meaning, or omit anything.\n"
    "3. Keep the original language(s) exactly as written (Telugu/English/Hindi).\n"
    "4. Only fix mechanical artefacts: broken line-wraps, doubled spaces, stray "
    "control characters; turn tabular rows into Markdown tables or lists.\n"
    "5. Output only the cleaned Markdown — no preamble, no commentary."
)


def _clean_user(text: str, lang_hint: str) -> str:
    hint = f" The text is mostly in: {lang_hint}." if lang_hint else ""
    return f"Raw extracted text follows.{hint}\n\n{text}"


class SarvamLLM:
    def __init__(self, model: str):
        self.model = model

    def complete(self, system: str, user: str, temperature: float = 0.2,
                 max_tokens: int = 2048) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        resp = client().chat.completions(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
        return _unwrap(resp, "content", "text").strip()

    def clean_document(self, text: str, lang_hint: str = "") -> str:
        """Reformat raw office-doc text to clean Markdown, preserving all content.
        Sized generously so long press releases are not truncated."""
        text = (text or "").strip()
        if not text:
            return ""
        budget = min(8192, max(2048, len(text)))
        return self.complete(_CLEAN_SYSTEM, _clean_user(text, lang_hint),
                             temperature=0.0, max_tokens=budget)


class AnthropicLLM:
    def __init__(self, model: str):
        import anthropic
        self.model = model
        self._client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY / _BASE_URL

    def complete(self, system: str, user: str, temperature: float = 0.2,
                 max_tokens: int = 2048) -> str:
        resp = self._client.messages.create(
            model=self.model, system=system or None,
            messages=[{"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens,
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    def clean_document(self, text: str, lang_hint: str = "") -> str:
        text = (text or "").strip()
        if not text:
            return ""
        budget = min(8192, max(2048, len(text)))
        return self.complete(_CLEAN_SYSTEM, _clean_user(text, lang_hint),
                             temperature=0.0, max_tokens=budget)
