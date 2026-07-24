"""LLM providers for entity/relation extraction and grounded synopsis.

Sarvam (sarvam-m / sarvam-30b / sarvam-105b) is primary; Anthropic optional.
"""
from __future__ import annotations

from .sarvam import _unwrap, client


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
