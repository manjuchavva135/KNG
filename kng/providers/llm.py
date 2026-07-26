"""LLM providers for entity/relation extraction and grounded synopsis.

Sarvam (sarvam-m / sarvam-30b / sarvam-105b) is primary; Anthropic optional.

`complete_json` is the structured path WP3's graph extraction runs on. It forces
a **tool call** rather than asking for JSON in prose, because a 4000-call pass
cannot afford a few percent of unparseable replies, and both providers expose
tool use with a JSON schema. Prose parsing survives only as a fallback.

Every call is retried with exponential backoff and counted on the provider
instance (`.calls`, `.retries`, `.failures`), so a paid pass can report exactly
what it spent instead of estimating.
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Optional

from ..config import settings
from .sarvam import _unwrap, chat_completion, client

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)

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


def parse_json_object(raw: str) -> Optional[dict]:
    """Best-effort JSON object out of a model reply.

    Only the fallback path — a forced tool call returns clean arguments. Models
    still wrap JSON in fences or a sentence of preamble, so try the fence, then
    the outermost brace pair, before giving up.
    """
    if not raw:
        return None
    for candidate in (raw, *(m.group(1) for m in _FENCE.finditer(raw))):
        candidate = candidate.strip()
        try:
            val = json.loads(candidate)
            if isinstance(val, dict):
                return val
        except (ValueError, TypeError):
            pass
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            val = json.loads(raw[start:end + 1])
            if isinstance(val, dict):
                return val
        except (ValueError, TypeError):
            pass
    return repair_truncated_json(raw)


def repair_truncated_json(raw: str) -> Optional[dict]:
    """Salvage a JSON object whose tail was cut off by an output-token cap.

    This is not a nicety on this deployment. `sarvam-105b` is a reasoning model
    that spends 2500-4000 tokens thinking before it emits anything, and the
    starter tier caps output at 4096 — so a long passage routinely returns a
    well-formed JSON *prefix* listing real entities, then stops mid-token. The
    alternative to repairing it is discarding a call that was already paid for.

    Walks the text tracking string/escape state, drops the incomplete trailing
    element, and closes whatever brackets are still open.
    """
    start = raw.find("{")
    if start < 0:
        return None
    stack: list[str] = []
    in_str = False
    escape = False
    last_safe = -1              # index just past the last completed array element
    for i, ch in enumerate(raw[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            # A closed object sitting directly inside an array is a complete
            # element; everything up to here can be kept.
            if ch == "}" and stack and stack[-1] == "]":
                last_safe = i + 1
        elif ch == "," and stack and stack[-1] == "]":
            last_safe = i

    for cut in (len(raw), last_safe):
        if cut <= start:
            continue
        body = raw[start:cut].rstrip().rstrip(",")
        depth: list[str] = []
        in_s = False
        esc = False
        for ch in body:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_s = not in_s
                continue
            if in_s:
                continue
            if ch in "{[":
                depth.append("}" if ch == "{" else "]")
            elif ch in "}]" and depth:
                depth.pop()
        if in_s:
            body += '"'
        candidate = body + "".join(reversed(depth))
        try:
            val = json.loads(candidate)
            if isinstance(val, dict):
                return val
        except (ValueError, TypeError):
            continue
    return None


def _is_permanent(exc: Exception) -> bool:
    """True for client errors that will fail identically on every retry.

    408 (timeout) and 429 (rate limit) are excluded — those are exactly the ones
    backing off does fix.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status is None:
        resp = getattr(exc, "response", None)          # httpx.HTTPStatusError
        status = getattr(resp, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500 and status not in (408, 429)


class _Retrying:
    """Shared retry/accounting behaviour for both providers."""

    def __init__(self) -> None:
        self.calls = 0
        self.retries = 0
        self.failures = 0
        self.truncated = 0        # replies salvaged from an output-cap cut-off
        self.last_error: str = ""

    def _attempt(self, fn, *, retries: Optional[int] = None):
        """Run `fn` with exponential backoff + jitter; None when it never lands.

        Jitter matters at concurrency: without it, a burst of workers that hit
        one rate-limit response would retry in lockstep and hit it again.
        """
        s = settings()
        budget = s.llm_retries if retries is None else retries
        for attempt in range(budget + 1):
            try:
                self.calls += 1
                return fn()
            except Exception as e:                    # provider SDKs raise varied types
                self.last_error = f"{type(e).__name__}: {e}"
                if _is_permanent(e):
                    # A bad request never becomes a good one. Retrying a
                    # deprecated model or a malformed schema just multiplies the
                    # wait before the real error surfaces — which is exactly what
                    # buried the `sarvam-m` deprecation notice behind four
                    # backoff rounds per chunk.
                    self.failures += 1
                    return None
                if attempt >= budget:
                    self.failures += 1
                    return None
                self.retries += 1
                time.sleep(min(30.0, 2.0 ** attempt) + random.uniform(0, 1.0))
        return None


class SarvamLLM(_Retrying):
    def __init__(self, model: str):
        super().__init__()
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

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *,
                      name: str = "record", description: str = "",
                      max_tokens: int = 1024,
                      retries: Optional[int] = None) -> Optional[dict]:
        """Forced tool call → parsed arguments. None when every attempt failed."""
        s = settings()
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        tool = {"type": "function",
                "function": {"name": name,
                             "description": description or f"Return the {name}.",
                             "parameters": schema}}

        payload = {
            "model": self.model, "messages": messages, "temperature": 0.0,
            "max_tokens": max_tokens, "seed": s.llm_seed,
            # "auto", not "required". Forcing the tool on sarvam-105b makes it
            # emit nothing at all — it reasons until the token cap and returns
            # an empty message. Left to choose, it reliably writes the same JSON
            # as prose, which the parser below handles.
            "tools": [tool], "tool_choice": "auto",
        }
        effort = (s.llm_reasoning_effort or "").strip().lower()
        # "null"/"none"/"" -> send JSON null, which the API accepts to disable
        # reasoning; anything else must be one of low|medium|high.
        payload["reasoning_effort"] = None if effort in ("", "null", "none") else effort

        def _call():
            return chat_completion(payload)

        resp = self._attempt(_call, retries=retries)
        if resp is None:
            return None
        args = _tool_arguments(resp)
        if args is not None:
            return args
        # Prose fallback. Use the strict unwrap: `_unwrap`'s `str(resp)`
        # fallthrough would hand back a stringified SDK object, which parses as
        # nothing and would otherwise be indistinguishable from a genuinely
        # empty extraction. `parse_json_object` repairs a cap-truncated tail.
        text = _unwrap_strict(resp)
        if not text:
            self.last_error = (
                f"empty message (finish_reason={_finish_reason(resp)}) — the model "
                f"consumed max_tokens={max_tokens} without emitting output")
            return None
        out = parse_json_object(text)
        if out is None:
            self.last_error = f"unparseable reply ({len(text)} chars)"
        elif _finish_reason(resp) == "length":
            self.truncated += 1
        return out


def _finish_reason(resp) -> str:
    choices = resp.get("choices") if isinstance(resp, dict) else getattr(resp, "choices", None)
    if not choices:
        return ""
    c0 = choices[0]
    return str(getattr(c0, "finish_reason", None)
               or (c0.get("finish_reason") if isinstance(c0, dict) else "") or "")


def _unwrap_strict(resp) -> str:
    """Message text, or "" — never a stringified response object.

    `_unwrap` ends in `return str(resp)`, which turns an unexpected shape into
    something that looks like model output. That is survivable for document
    cleanup and not for graph extraction, where it would become entities.
    """
    def _get(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    choices = _get(resp, "choices")
    if not choices:
        return ""
    msg = _get(choices[0], "message")
    content = _get(msg, "content") if msg is not None else None
    return content if isinstance(content, str) else ""


def _tool_arguments(resp) -> Optional[dict]:
    """Pull `choices[0].message.tool_calls[0].function.arguments` from either an
    SDK object or a plain dict, and parse it."""
    def _get(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    choices = _get(resp, "choices")
    if not choices:
        return None
    msg = _get(choices[0], "message")
    calls = _get(msg, "tool_calls") if msg is not None else None
    if not calls:
        return None
    fn = _get(calls[0], "function")
    raw = _get(fn, "arguments") if fn is not None else None
    if isinstance(raw, dict):
        return raw
    return parse_json_object(raw or "")


class FakeLLM(_Retrying):
    """Offline test double — `KNG_FAKE_LLM=1`. Makes no network calls.

    The guardrail on this repo is that paid passes belong to the user, which
    would otherwise leave the entire extraction path — cache, concurrency,
    resume, validation, resolution, graph loading — shipping untested. This
    returns deterministic records built from the ontology's own alias table
    scanned over the passage, so the free verification exercises real code with
    real (if shallow) data.
    """

    def __init__(self, model: str = "fake"):
        super().__init__()
        self.model = model

    def complete(self, system: str, user: str, temperature: float = 0.2,
                 max_tokens: int = 2048) -> str:
        """Extractive stand-in for WP4 synthesis — deterministic, offline.

        Returns the opening sentence of the first few numbered SOURCES with
        their `[n]` markers attached, which exercises the real path (prompt
        assembly, citation verification, source rendering) without a paid call.
        Labelled in the text so fixture output can never be read as an answer.
        """
        self.calls += 1
        sources = _FAKE_SOURCE.findall(user or "")
        if not sources:
            return ""
        lines = ["(offline fixture answer — KNG_FAKE_LLM=1, no model was called)", ""]
        for n, body in sources[:4]:
            sentence = " ".join(body.split())[:220]
            lines.append(f"{sentence} [{n}]")
        return "\n".join(lines)

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *,
                      name: str = "record", description: str = "",
                      max_tokens: int = 1024,
                      retries: Optional[int] = None) -> Optional[dict]:
        self.calls += 1
        if name == "record_community":
            return {"title": "fake cluster", "summary": "Offline placeholder summary."}

        from ..graph import ontology as onto
        haystack = onto.normalise(user)
        found: list[dict] = []
        for variant, canonical in onto.alias_map().items():
            if variant and variant in haystack:
                etype = _FAKE_TYPES.get(canonical, "Organization")
                if etype in onto.extractable_types():
                    found.append({"name": canonical, "type": etype, "english_name": ""})
        uniq = {f["name"]: f for f in found}
        ents = list(uniq.values())
        rels = []
        people = [e for e in ents if e["type"] == "Person"]
        others = [e for e in ents if e["type"] in ("Party", "Organization")]
        if people and others:
            rels.append({"source": people[0]["name"], "relation": "ACCUSES",
                         "target": others[0]["name"], "evidence": "offline fixture"})
        return {"entities": ents, "relations": rels}


# Numbered source blocks as `kng.generation.synthesize` renders them:
# "[3] (passage) citation…\n<text>".
_FAKE_SOURCE = re.compile(r"^\[(\d+)\][^\n]*\n(.+?)(?=\n\[\d+\]|\n\n|\Z)", re.S | re.M)

_FAKE_TYPES = {
    "Y. S. Jagan Mohan Reddy": "Person", "N. Chandrababu Naidu": "Person",
    "Nara Lokesh": "Person", "YSRCP": "Party", "TDP": "Party",
    "TTD": "Organization", "Amaravati": "Place",
}


class AnthropicLLM(_Retrying):
    def __init__(self, model: str):
        import anthropic
        super().__init__()
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

    def complete_json(self, system: str, user: str, schema: dict[str, Any], *,
                      name: str = "record", description: str = "",
                      max_tokens: int = 1024,
                      retries: Optional[int] = None) -> Optional[dict]:
        tool = {"name": name, "description": description or f"Return the {name}.",
                "input_schema": schema}

        def _call():
            return self._client.messages.create(
                model=self.model, system=system or None,
                messages=[{"role": "user", "content": user}],
                max_tokens=max_tokens, temperature=0.0,
                tools=[tool], tool_choice={"type": "tool", "name": name},
            )

        resp = self._attempt(_call, retries=retries)
        if resp is None:
            return None
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                return dict(block.input)
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        return parse_json_object(text)
