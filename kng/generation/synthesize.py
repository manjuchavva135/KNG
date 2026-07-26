"""Grounded synopsis with exact citations — the deliverable of the whole project.

Two rules shape everything here:

1. **Nothing enters the answer that is not in the numbered source list.** The
   model is given passages and graph facts, each with an `[n]`, and told that a
   sentence without an `[n]` is a defect. Claims about a politician's statements
   are exactly where an ungrounded model sentence does real damage.
2. **Citations are verified after generation, not trusted.** `[9]` in a
   five-source answer is a hallucinated citation; it is stripped and counted, so
   a bad answer looks bad instead of looking sourced.

Passages and facts share one numbering scheme because a reader does not care
whether a claim came from a chunk or an edge — they care which file it is in and
what page or timestamp. Both resolve to a citation string built in WP1/WP3.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..config import settings
from ..retrieval import Context, hybrid, retrieve

# Per-source text budget. bge-m3 chunks run to ~1000 tokens; the whole chunk is
# rarely needed to support one claim, and sarvam-105b's starter tier caps output
# at 4096 tokens, so prompt bulk directly costs answer length.
MAX_PASSAGE_CHARS = 1100
MAX_PROMPT_CHARS = 16000

_CITE = re.compile(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]")

_LANG_NAME = {"te": "Telugu", "en": "English", "hi": "Hindi",
              "mixed": "the question's own mix of languages"}

_SYSTEM = """You are a research assistant over an archive of YS Jagan Mohan Reddy's press meets \
(Andhra Pradesh politics). The material is Telugu, English and Hindi.

You answer ONLY from the numbered SOURCES given to you.

Rules:
1. Every factual sentence must end with the source number(s) it comes from, like [2] or [1, 4].
2. Never state anything the sources do not state. Do not add background knowledge, \
do not infer motives, do not fill gaps.
3. If the sources do not answer the question, say so plainly and describe what they do cover. \
A short honest answer is correct; a padded one is not.
4. When sources disagree, report the disagreement and cite both.
5. Attribute claims to who made them ("Jagan alleged ...", "the Sakshi report states ..."), \
because this archive is mostly one politician's assertions, not established fact.
6. Prefer specifics from the sources — names, dates, amounts, scheme names — over generalities.
7. GRAPH FACTS are relations previously extracted from the same archive; treat them exactly \
like passages and cite them the same way.
8. Write {language}. Keep proper nouns in their usual spelling.
9. Structure: a two-to-four sentence direct answer, then '## Details' with the supporting \
points, then '## Timeline' only if dated events matter to the question.
"""

_USER = """QUESTION: {question}

SOURCES
{sources}
{extras}
Write the grounded answer now. Cite with [n] after every factual sentence."""


@dataclass
class Answer:
    question: str
    text: str = ""
    sources: list[dict] = field(default_factory=list)
    cited: list[int] = field(default_factory=list)
    uncited_sentences: int = 0
    invalid_citations: list[int] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def build_sources(ctx: Context, max_passage_chars: int = MAX_PASSAGE_CHARS) -> list[dict]:
    """One numbered list covering passages and graph facts.

    Passages come first: they are verbatim archive text, and a fact is a
    compressed restatement of something a passage already says.
    """
    sources: list[dict] = []
    for p in ctx.passages:
        sources.append({
            "n": len(sources) + 1,
            "kind": "passage",
            "citation": p.get("citation", ""),
            "source_file": p.get("source_file", ""),
            "press_meet_id": p.get("press_meet_id", ""),
            "date": p.get("date"),
            "language": p.get("language", ""),
            "source_type": p.get("source_type", ""),
            "publication": p.get("publication"),
            "text": _clip(p.get("text", ""), max_passage_chars),
            "score": round(float(p.get("score", 0.0)), 5),
            "ranks": p.get("ranks", {}),
            "duplicates": p.get("duplicates", []),
        })
    for f in ctx.facts:
        ev = f.get("evidence") or [{}]
        citation = ev[0].get("citation", "") or f"graph: {len(f.get('press_meet_ids', []))} meet(s)"
        span = f.get("first_date") or ""
        if f.get("last_date") and f.get("last_date") != span:
            span = f"{span}→{f['last_date']}"
        quote = _clip(ev[0].get("quote", ""), 300)
        sources.append({
            "n": len(sources) + 1,
            "kind": "fact",
            "citation": citation,
            "press_meet_id": (f.get("press_meet_ids") or [""])[0],
            "date": f.get("first_date"),
            "text": (f"{f['source']} [{f['source_type']}] --{f['relation']}--> "
                     f"{f['target']} [{f['target_type']}] "
                     f"(asserted {f['weight']}×{', ' + span if span else ''})"
                     + (f'\n   quote: "{quote}"' if quote else "")),
            "relation": f["relation"],
            "structural": f.get("structural", False),
        })
    return sources


def _render_sources(sources: list[dict], budget: int = MAX_PROMPT_CHARS) -> str:
    """Numbered evidence block, truncated at a character budget.

    Truncation drops the lowest-ranked sources rather than shortening every
    passage: half a passage is half a citation.
    """
    lines: list[str] = []
    used = 0
    for s in sources:
        head = f"[{s['n']}] ({s['kind']}) {s['citation']}"
        if s.get("date"):
            head += f"  date={s['date']}"
        body = f"{head}\n{s['text']}\n"
        if used + len(body) > budget and lines:
            break
        lines.append(body)
        used += len(body)
    return "\n".join(lines)


def _render_extras(ctx: Context) -> str:
    """Timeline and community context — orientation, not citable claims."""
    out: list[str] = []
    if ctx.timeline:
        rows = "\n".join(
            f"  {r.get('date') or '(undated)'}  meet {r.get('press_meet_id', '')}  "
            f"{_clip(r.get('title', ''), 70)}"
            for r in ctx.timeline)
        out.append("\nMEETS THIS TOPIC APPEARS IN (for ordering only — cite the "
                   f"sources above, not this list):\n{rows}")
    for c in ctx.communities:
        if c.get("summary"):
            out.append(f"\nTOPIC CLUSTER — {c.get('title') or c['community_id']}: "
                       f"{_clip(c['summary'], 600)}")
    return "\n".join(out) + ("\n" if out else "")


def answer_language(question: str) -> str:
    """Configured answer language, or the question's own when set to `auto`."""
    configured = (settings().answer_language or "auto").strip().lower()
    if configured and configured != "auto":
        return _LANG_NAME.get(configured, configured)
    from ..pipeline.normalize import detect_language
    return _LANG_NAME.get(detect_language(question) or "en", "English")


def build_prompt(ctx: Context, sources: list[dict]) -> tuple[str, str]:
    system = _SYSTEM.format(language=answer_language(ctx.question))
    user = _USER.format(question=ctx.question,
                        sources=_render_sources(sources),
                        extras=_render_extras(ctx))
    return system, user


def verify_citations(text: str, sources: list[dict]) -> tuple[str, list[int], list[int], int]:
    """Strip citations that point at nothing; count unsupported sentences.

    Returns `(clean_text, cited, invalid, uncited_sentences)`. Invalid markers
    are removed rather than left in place: a reader cannot tell a fabricated
    `[9]` from a real one, and leaving it makes the answer look better sourced
    than it is.
    """
    valid = {s["n"] for s in sources}
    cited: list[int] = []
    invalid: list[int] = []

    def _sub(m: re.Match) -> str:
        nums = [int(x) for x in m.group(1).replace(" ", "").split(",")]
        good = [n for n in nums if n in valid]
        invalid.extend(n for n in nums if n not in valid)
        for n in good:
            if n not in cited:
                cited.append(n)
        return f"[{', '.join(str(n) for n in good)}]" if good else ""

    clean = _CITE.sub(_sub, text or "")
    uncited = 0
    for sentence in re.split(r"(?<=[.!?।])\s+|\n+", clean):
        s = sentence.strip()
        if len(s) < 40 or s.startswith("#") or (s.startswith("-") and len(s) < 60):
            continue                        # headings and short list labels
        if not _CITE.search(s):
            uncited += 1
    return clean.strip(), sorted(cited), sorted(set(invalid)), uncited


def answer(question: str, k: int = 8, *,
           filters: Optional[hybrid.Filters] = None,
           use_graph: bool = True, graph_hops: int = 1,
           max_tokens: int = 1600,
           ctx: Optional[Context] = None) -> Answer:
    """Retrieve, then synthesise a cited answer. One LLM call.

    Retrieval is free; only this call costs money, which is why the context can
    be inspected (`kng.answer --retrieval-only`) before anything is spent.
    """
    ctx = ctx if ctx is not None else retrieve(
        question, k=k, filters=filters, use_graph=use_graph, graph_hops=graph_hops)
    sources = build_sources(ctx)
    result = Answer(question=question, sources=sources, diagnostics=dict(ctx.diagnostics))
    if not sources:
        result.text = "No passage in the archive matches this question."
        result.diagnostics["generation"] = "skipped — nothing retrieved"
        return result

    from ..providers import get_llm
    llm = get_llm()
    system, user = build_prompt(ctx, sources)
    raw = llm.complete(system, user, temperature=0.1, max_tokens=max_tokens)
    result.diagnostics["llm"] = {
        "model": getattr(llm, "model", "?"), "calls": getattr(llm, "calls", 0),
        "retries": getattr(llm, "retries", 0), "failures": getattr(llm, "failures", 0),
        "prompt_chars": len(system) + len(user),
    }
    if not raw:
        result.text = ""
        result.diagnostics["generation"] = (
            f"empty reply — {getattr(llm, 'last_error', '') or 'no error recorded'}")
        return result

    clean, cited, invalid, uncited = verify_citations(raw, sources)
    result.text = clean
    result.cited = cited
    result.invalid_citations = invalid
    result.uncited_sentences = uncited
    # Only the sources the answer actually used are worth rendering under it;
    # the rest stay in `sources` for inspection.
    result.diagnostics["sources_used"] = f"{len(cited)} of {len(sources)}"
    return result
