"""CLI for the evaluation harness.

    python -m kng.eval                          # free: retrieval scores only
    python -m kng.eval -k 12                    # sweep k
    python -m kng.eval --no-graph               # ablate the graph leg
    python -m kng.eval --only laddu-en,seci-tariff
    python -m kng.eval --baseline var/eval/20260726-120000-k8.json
    KNG_FAKE_LLM=1 python -m kng.eval --answer  # end-to-end, offline, free
    python -m kng.eval --answer --spend         # PAID: up to 3 calls per question

`--answer` against a real provider needs `--spend` as well. The project's standing
rule is that paid passes are run deliberately by the user, so the flag that costs
money is not the flag that is easy to type by accident.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import harness


def _progress(i: int, total: int, res: harness.QuestionResult) -> None:
    mark = "!" if res.error else ("✅" if res.meet_hit else "❌")
    extra = (
        f" cited={res.cited} {'REFUSED' if res.refused else 'GROUNDED' if res.grounding_passed else ''}"
        if res.answered else "")
    print(f"[{i}/{total}] {mark} {res.id:<22} rank={res.meet_rank or '—':<3} "
          f"facts={res.facts:<3} {res.retrieval_s:.2f}s{extra}"
          + (f"  {res.error}" if res.error else ""), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.eval", description=__doc__.split("\n")[0])
    ap.add_argument("-k", type=int, default=8, help="passages per question (default 8)")
    ap.add_argument("--no-graph", action="store_true", help="ablate the graph leg")
    ap.add_argument("--graph-hops", type=int, default=1)
    ap.add_argument("--answer", action="store_true",
                    help="also answer each question (up to 3 LLM calls each)")
    ap.add_argument("--spend", action="store_true",
                    help="confirm paid calls when --answer uses a real provider")
    ap.add_argument("--language", help="force the answer language (en|te)")
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--limit", type=int, help="first N questions only")
    ap.add_argument("--questions", type=Path, help="an alternative question file")
    ap.add_argument("--baseline", type=Path, help="an earlier run's JSON, for deltas")
    ap.add_argument("--out", type=Path, help="directory for the report (default var/eval)")
    ap.add_argument("--no-save", action="store_true", help="print only, write nothing")
    ap.add_argument("--json", action="store_true", help="print the raw JSON report")
    ap.add_argument("--meets", action="store_true",
                    help="list the press-meet ids the index has, then exit")
    ap.add_argument("--validate", action="store_true",
                    help="check every expectation names a real press meet, then exit")
    args = ap.parse_args(argv)

    if args.meets:
        for meet in sorted(harness.known_meets()):
            print(meet)
        return 0

    questions = harness.load_questions(args.questions)
    if args.only:
        wanted = {q.strip() for q in args.only.split(",") if q.strip()}
        missing = wanted - {q.id for q in questions}
        if missing:
            print(f"error: no such question id: {', '.join(sorted(missing))}",
                  file=sys.stderr)
            return 1
        questions = [q for q in questions if q.id in wanted]
    if args.limit:
        questions = questions[:args.limit]

    problems = harness.validate(questions)
    if problems:
        # A stale expectation reads as a permanent retrieval failure, so this is
        # fatal rather than a warning.
        print("error: the question set expects press meets the index does not have:",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    if args.validate:
        print(f"{len(questions)} questions, every expectation resolves")
        return 0

    if args.answer:
        from ..providers import get_llm
        provider = type(get_llm()).__name__
        if provider != "FakeLLM" and not args.spend:
            print(f"refusing to run up to {3 * len(questions)} paid calls through {provider} "
                  f"without --spend.\nFor a free end-to-end check: "
                  f"KNG_FAKE_LLM=1 python -m kng.eval --answer", file=sys.stderr)
            return 2
        if provider != "FakeLLM":
            print(f"⚠ up to {3 * len(questions)} paid {provider} calls\n", flush=True)

    report = harness.run(questions, k=args.k, use_graph=not args.no_graph,
                         graph_hops=args.graph_hops, with_answer=args.answer,
                         language=args.language, progress=_progress)

    baseline = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
    else:
        print()
        print(harness.markdown(report, baseline=baseline))

    if not args.no_save:
        js, md = harness.save(report, out_dir=args.out)
        print(f"written: {js}\n         {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
