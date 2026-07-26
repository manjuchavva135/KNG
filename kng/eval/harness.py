"""WP6 evaluation harness — does a retrieval or prompt change help, or only feel better?

Every earlier decision in this project that mattered was settled by measurement
(`scripts/bench_extract.py` turned a 17-hour graph pass into 73 minutes). Answer
quality had no such instrument: WP5 shipped `uncited_sentences` and
`invalid_citations` per answer into `var/queries.jsonl`, but nothing that runs a
*fixed* question set and produces a comparable number.

Two levels, because they cost different amounts:

* **retrieval (free, the default).** Does the evidence for a question include the
  press meet that actually holds the answer? Runs entirely on the local index and
  embedding model — no key, no network, no spend. This is what a reranker or a
  filter change has to move.
* **answer (opt-in, one paid call per question).** Adds citation coverage: how
  many sources the model cited, whether any `[n]` had to be stripped, how many
  sentences carry no citation, and — the strongest single signal — whether a
  *cited* source comes from an expected press meet.

Expectations are coarse on purpose: the right press meet, not the right chunk.
Nobody hand-labelled 4267 passages, and a chunk-level gold set invented here would
look precise while measuring the invention. See `questions.yaml`.
"""
from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ..config import settings
from ..retrieval import Context, hybrid, retrieve

QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.yaml"


# ── the question set ───────────────────────────────────────────────────────────
@dataclass
class Question:
    id: str
    q: str
    lang: str = "en"
    kind: str = "single"
    meets: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)


def load_questions(path: Optional[Path] = None) -> list[Question]:
    blob = yaml.safe_load((path or QUESTIONS_FILE).read_text(encoding="utf-8")) or {}
    out: list[Question] = []
    seen: set[str] = set()
    for raw in blob.get("questions", []):
        qid = str(raw.get("id", "")).strip()
        if not qid:
            raise ValueError("every question needs an id")
        if qid in seen:
            raise ValueError(f"duplicate question id: {qid}")
        seen.add(qid)
        out.append(Question(
            id=qid, q=str(raw["q"]).strip(), lang=raw.get("lang", "en"),
            kind=raw.get("kind", "single"),
            meets=[str(m) for m in (raw.get("meets") or [])],
            entities=[str(e) for e in (raw.get("entities") or [])]))
    if not out:
        raise ValueError("the question set is empty")
    return out


def known_meets() -> set[str]:
    """Press-meet ids present in the index, for validating expectations."""
    from ..api import meta                      # local: pulls in the chunk scan
    return {str(m["id"]) for m in meta.corpus_meta()["press_meets"]}


def validate(questions: Iterable[Question]) -> list[str]:
    """Expectations that name a press meet the index does not have.

    A typo here would read as a retrieval failure forever, so the CLI checks this
    before it reports anything.
    """
    have = known_meets()
    problems = []
    for q in questions:
        for meet in q.meets:
            if meet not in have:
                problems.append(f"{q.id}: no such press meet {meet!r}")
    return problems


# ── scoring ────────────────────────────────────────────────────────────────────
@dataclass
class QuestionResult:
    id: str
    question: str
    lang: str
    kind: str
    passages: int = 0
    facts: int = 0
    meet_hit: bool = False                  # any retrieved passage from an expected meet
    meet_rank: Optional[int] = None         # 1-based rank of the first such passage
    reciprocal_rank: float = 0.0
    meet_recall: float = 0.0                # expected meets covered / expected meets
    meet_precision: float = 0.0             # share of retrieved passages in an expected meet
    entity_hit: bool = False                # an expected entity was linked in the graph
    entities_linked: list[str] = field(default_factory=list)
    retrieval_s: float = 0.0
    top: list[dict] = field(default_factory=list)
    # answer mode only
    answered: bool = False
    answer_chars: int = 0
    cited: int = 0
    invalid_citations: list[int] = field(default_factory=list)
    uncited_sentences: int = 0
    cited_expected_meet: bool = False
    answer_s: float = 0.0
    error: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def score_retrieval(question: Question, ctx: Context) -> QuestionResult:
    expected = set(question.meets)
    res = QuestionResult(id=question.id, question=question.q, lang=question.lang,
                         kind=question.kind, passages=len(ctx.passages),
                         facts=len(ctx.facts))

    hits = 0
    covered: set[str] = set()
    for i, p in enumerate(ctx.passages, start=1):
        meet = str(p.get("press_meet_id") or "")
        if meet in expected:
            hits += 1
            covered.add(meet)
            if res.meet_rank is None:
                res.meet_rank = i
        if i <= 5:
            res.top.append({"rank": i, "meet": meet,
                            "citation": p.get("citation", ""),
                            "source_type": p.get("source_type", ""),
                            "language": p.get("language", "")})

    res.meet_hit = res.meet_rank is not None
    res.reciprocal_rank = 1.0 / res.meet_rank if res.meet_rank else 0.0
    res.meet_recall = len(covered) / len(expected) if expected else 0.0
    res.meet_precision = hits / len(ctx.passages) if ctx.passages else 0.0

    # Entity linking: the graph leg is only useful if the question's subject was
    # recognised. Match on substrings both ways — "TTD" should link "TTD Laddu",
    # and an expected "laddu" should match "TTD Laddu" too.
    names = [str(e.get("name", "")) for e in ctx.entities]
    res.entities_linked = [n for n in names if n]
    wanted = [_norm(e) for e in question.entities if e.strip()]
    haystack = [_norm(n) for n in names]
    res.entity_hit = any(w and any(w in h or h in w for h in haystack) for w in wanted)
    return res


def score_answer(question: Question, res: QuestionResult, ans) -> QuestionResult:
    """Fold answer-level metrics into a result that already has retrieval scores."""
    res.answered = True
    res.answer_chars = len(ans.text or "")
    res.cited = len(ans.cited or [])
    res.invalid_citations = list(ans.invalid_citations or [])
    res.uncited_sentences = int(ans.uncited_sentences or 0)

    expected = set(question.meets)
    by_n = {s.get("n"): s for s in (ans.sources or [])}
    for n in (ans.cited or []):
        src = by_n.get(n) or {}
        if str(src.get("press_meet_id") or "") in expected:
            res.cited_expected_meet = True
            break
    return res


# ── running ────────────────────────────────────────────────────────────────────
@dataclass
class Report:
    config: dict[str, Any] = field(default_factory=dict)
    results: list[QuestionResult] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"config": self.config,
                "aggregate": self.aggregate,
                "results": [asdict(r) for r in self.results]}


def _mean(values: list[float]) -> Optional[float]:
    return round(statistics.fmean(values), 3) if values else None


def aggregate(results: list[QuestionResult]) -> dict[str, Any]:
    answered = [r for r in results if r.answered]
    out: dict[str, Any] = {
        "questions": len(results),
        "errors": sum(1 for r in results if r.error),
        "meet_hit_rate": _mean([1.0 if r.meet_hit else 0.0 for r in results]),
        "mrr": _mean([r.reciprocal_rank for r in results]),
        "meet_recall": _mean([r.meet_recall for r in results]),
        "meet_precision": _mean([r.meet_precision for r in results]),
        "entity_hit_rate": _mean([1.0 if r.entity_hit else 0.0 for r in results]),
        "mean_facts": _mean([float(r.facts) for r in results]),
        "mean_retrieval_s": _mean([r.retrieval_s for r in results]),
        "misses": [r.id for r in results if not r.meet_hit],
    }
    # Cut by kind and by script: a single mean hides that cross-meet questions or
    # Telugu questions are the weak ones, which is exactly what needs fixing.
    for key, attr in (("by_kind", "kind"), ("by_lang", "lang")):
        buckets: dict[str, list[QuestionResult]] = {}
        for r in results:
            buckets.setdefault(getattr(r, attr), []).append(r)
        out[key] = {
            name: {"n": len(rs),
                   "meet_hit_rate": _mean([1.0 if r.meet_hit else 0.0 for r in rs]),
                   "mrr": _mean([r.reciprocal_rank for r in rs])}
            for name, rs in sorted(buckets.items())
        }
    if answered:
        out["answers"] = {
            "n": len(answered),
            "mean_cited": _mean([float(r.cited) for r in answered]),
            "mean_uncited_sentences": _mean([float(r.uncited_sentences) for r in answered]),
            "answers_with_stripped_citations":
                sum(1 for r in answered if r.invalid_citations),
            "cited_expected_meet_rate":
                _mean([1.0 if r.cited_expected_meet else 0.0 for r in answered]),
            "mean_answer_s": _mean([r.answer_s for r in answered]),
        }
    return out


def run(questions: list[Question], *, k: int = 8, use_graph: bool = True,
        graph_hops: int = 1, with_answer: bool = False,
        language: Optional[str] = None,
        filters: Optional[hybrid.Filters] = None,
        progress=None) -> Report:
    """Score every question. Retrieval is free; `with_answer` costs one call each."""
    results: list[QuestionResult] = []
    for i, q in enumerate(questions, start=1):
        started = time.monotonic()
        try:
            ctx = retrieve(q.q, k=k, filters=filters, use_graph=use_graph,
                           graph_hops=graph_hops)
        except Exception as e:                 # one bad question must not end the run
            res = QuestionResult(id=q.id, question=q.q, lang=q.lang, kind=q.kind,
                                 error=f"{type(e).__name__}: {e}")
            results.append(res)
            if progress:
                progress(i, len(questions), res)
            continue

        res = score_retrieval(q, ctx)
        res.retrieval_s = round(time.monotonic() - started, 3)

        if with_answer:
            from ..generation.synthesize import answer as synthesize
            t0 = time.monotonic()
            try:
                # Reuse the context that was just scored: the answer must be judged
                # on the same evidence, and re-retrieving would double the work.
                ans = synthesize(q.q, k=k, use_graph=use_graph,
                                 graph_hops=graph_hops, language=language, ctx=ctx)
                res = score_answer(q, res, ans)
            except Exception as e:
                res.error = f"{type(e).__name__}: {e}"
            res.answer_s = round(time.monotonic() - t0, 3)

        results.append(res)
        if progress:
            progress(i, len(questions), res)

    report = Report(results=results)
    report.aggregate = aggregate(results)
    report.config = {
        "k": k, "use_graph": use_graph, "graph_hops": graph_hops,
        "with_answer": with_answer, "language": language,
        "questions_file": str(QUESTIONS_FILE),
        "embed_model": settings().local_embed_model,
        "rerank_provider": settings().rerank_provider,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if with_answer:
        from ..providers import get_llm
        llm = get_llm()
        report.config["llm"] = type(llm).__name__
        report.config["llm_model"] = getattr(llm, "model", None)
    return report


# ── reporting ──────────────────────────────────────────────────────────────────
def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _delta(now: Any, before: Any) -> str:
    if not isinstance(now, (int, float)) or not isinstance(before, (int, float)):
        return ""
    diff = now - before
    if abs(diff) < 1e-9:
        return " (=)"
    return f" ({diff:+.3f})"


def markdown(report: Report, baseline: Optional[dict] = None) -> str:
    agg = report.aggregate
    base = (baseline or {}).get("aggregate", {}) if baseline else {}
    cfg = report.config
    lines = [
        f"# KNG eval — {cfg.get('ran_at', '')}",
        "",
        f"`k={cfg.get('k')}` · graph={'on' if cfg.get('use_graph') else 'off'} · "
        f"embed=`{cfg.get('embed_model')}` · rerank=`{cfg.get('rerank_provider')}`"
        + (f" · llm=`{cfg.get('llm_model') or cfg.get('llm')}`" if cfg.get("with_answer") else ""),
        "",
        "## Retrieval",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    for label, key in (("questions", "questions"),
                       ("meet hit rate", "meet_hit_rate"),
                       ("MRR", "mrr"),
                       ("meet recall", "meet_recall"),
                       ("meet precision", "meet_precision"),
                       ("entity link rate", "entity_hit_rate"),
                       ("mean graph facts", "mean_facts"),
                       ("mean retrieval s", "mean_retrieval_s"),
                       ("errors", "errors")):
        lines.append(f"| {label} | {_fmt(agg.get(key))}{_delta(agg.get(key), base.get(key))} |")

    for title, key in (("By kind", "by_kind"), ("By script", "by_lang")):
        lines += ["", f"### {title}", "", "| group | n | meet hit | MRR |", "|---|---:|---:|---:|"]
        for name, cut in (agg.get(key) or {}).items():
            lines.append(f"| {name} | {cut['n']} | {_fmt(cut['meet_hit_rate'])} | "
                         f"{_fmt(cut['mrr'])} |")

    if agg.get("answers"):
        a = agg["answers"]
        b = (base.get("answers") or {})
        lines += ["", "## Answers", "", "| metric | value |", "|---|---:|"]
        for label, key in (("answered", "n"),
                           ("mean sources cited", "mean_cited"),
                           ("cited an expected meet", "cited_expected_meet_rate"),
                           ("mean uncited sentences", "mean_uncited_sentences"),
                           ("answers with stripped citations", "answers_with_stripped_citations"),
                           ("mean answer s", "mean_answer_s")):
            lines.append(f"| {label} | {_fmt(a.get(key))}{_delta(a.get(key), b.get(key))} |")

    lines += ["", "## Per question", "",
              "| id | kind | lang | hit | rank | recall | facts | entity |"
              + (" cited | uncited |" if cfg.get("with_answer") else ""),
              "|---|---|---|---|---:|---:|---:|---|"
              + ("---:|---:|" if cfg.get("with_answer") else "")]
    for r in report.results:
        row = (f"| {r.id} | {r.kind} | {r.lang} | {'✅' if r.meet_hit else '❌'} | "
               f"{r.meet_rank or '—'} | {_fmt(r.meet_recall)} | {r.facts} | "
               f"{'✅' if r.entity_hit else '·'} |")
        if cfg.get("with_answer"):
            row += (f" {r.cited} | {r.uncited_sentences} |"
                    if r.answered else " — | — |")
        lines.append(row)

    misses = agg.get("misses") or []
    if misses:
        lines += ["", "## Misses — the questions worth reading", ""]
        for r in report.results:
            if r.meet_hit or r.error:
                continue
            lines.append(f"**{r.id}** — expected one of "
                         f"{', '.join(m for m in _expected_of(report, r.id)) or '—'}; got:")
            for t in r.top[:3]:
                lines.append(f"  - rank {t['rank']} · meet `{t['meet']}` · "
                             f"{t['source_type']} · {t['citation']}")
            lines.append("")
    errors = [r for r in report.results if r.error]
    if errors:
        lines += ["", "## Errors", ""] + [f"- **{r.id}** — {r.error}" for r in errors]
    return "\n".join(lines) + "\n"


def _expected_of(report: Report, qid: str) -> list[str]:
    """Expected meets for a question id, read back from the set (for the report)."""
    for q in load_questions():
        if q.id == qid:
            return q.meets
    return []


def save(report: Report, out_dir: Optional[Path] = None) -> tuple[Path, Path]:
    """Write `<ts>.json` + `<ts>.md`. Defaults under `var/eval/` (git-ignored)."""
    directory = out_dir or (settings().path(settings().var_dir) / "eval")
    directory.mkdir(parents=True, exist_ok=True)
    stem = (f"{time.strftime('%Y%m%d-%H%M%S')}-k{report.config.get('k')}"
            f"{'-answer' if report.config.get('with_answer') else ''}"
            f"{'' if report.config.get('use_graph') else '-nograph'}")
    js = directory / f"{stem}.json"
    md = directory / f"{stem}.md"
    js.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=1),
                  encoding="utf-8")
    md.write_text(markdown(report), encoding="utf-8")
    return js, md


__all__ = ["Question", "QuestionResult", "Report", "load_questions", "validate",
           "known_meets", "score_retrieval", "score_answer", "run", "aggregate",
           "markdown", "save", "QUESTIONS_FILE"]
