"""GraphRAG question answering — WP4's user-facing command.

    python -m kng.answer "What did Jagan say about the TTD laddu row?"
    python -m kng.answer "SECI power deal" -k 12 --since 2024-01-01 --evidence
    python -m kng.answer "మద్యం కుంభకోణం" --retrieval-only     # free, no LLM call
    KNG_FAKE_LLM=1 python -m kng.answer "TTD laddu"            # offline dry run

Retrieval is free and local. A successful answer uses up to three provider calls
(sufficiency, synthesis, claim validation), so `--retrieval-only` shows exactly
what would be sent before anything is spent.
"""
from __future__ import annotations

import argparse
import json
import sys

from .generation import synthesize
from .retrieval import hybrid, retrieve


def _filters(args) -> hybrid.Filters:
    return hybrid.Filters(
        language=args.lang, source_type=args.source_type,
        press_meet_id=args.meet, publication=args.publication,
        since=args.since, until=args.until)


def _print_sources(sources: list[dict], cited: list[int], chars: int,
                   show_all: bool) -> None:
    shown = [s for s in sources if show_all or not cited or s["n"] in cited]
    if not shown:
        return
    print("\nSOURCES")
    for s in shown:
        mark = " " if not cited or s["n"] in cited else "·"
        print(f"{mark}[{s['n']}] {s['citation']}"
              + (f"  ({s['kind']})" if s["kind"] != "passage" else ""))
        if chars:
            print(f"      {synthesize._clip(s['text'], chars)}")
        for dup in s.get("duplicates", [])[:2]:
            print(f"      also in: {dup}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.answer",
                                 description="grounded, cited answer over the press-meet archive")
    ap.add_argument("question")
    ap.add_argument("-k", type=int, default=12, help="passages to retrieve")
    ap.add_argument("--lang", help="filter passages: te | en | hi | mixed")
    ap.add_argument("--source-type",
                    help="press_release | source_doc | news_clip | video | slide | table")
    ap.add_argument("--meet", help="press_meet_id")
    ap.add_argument("--publication", help="Sakshi | Eenadu | …")
    ap.add_argument("--since", help="ISO date lower bound")
    ap.add_argument("--until", help="ISO date upper bound")
    ap.add_argument("--no-graph", action="store_true", help="passages only")
    ap.add_argument("--no-keyword", action="store_true", help="skip the BM25 leg")
    ap.add_argument("--no-vector", action="store_true", help="skip the dense leg")
    ap.add_argument("--hops", type=int, default=1, help="graph expansion hops")
    ap.add_argument("--max-facts", type=int, default=20)
    ap.add_argument("--retrieval-only", action="store_true",
                    help="show the evidence and make no LLM call (free)")
    ap.add_argument("--prompt", action="store_true",
                    help="with --retrieval-only, print the exact prompt instead")
    ap.add_argument("--all-sources", action="store_true",
                    help="list retrieved sources the answer did not cite too")
    ap.add_argument("--chars", type=int, default=0,
                    help="characters of each source to print (0 = citations only)")
    ap.add_argument("--max-tokens", type=int, default=1600)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--build-fts", action="store_true",
                    help="build the BM25 index over index/lancedb first (free, one-off)")
    args = ap.parse_args(argv)

    if args.build_fts:
        try:
            print(hybrid.ensure_fts_index(), file=sys.stderr)
        except Exception as e:
            print(f"could not build fts index: {e}", file=sys.stderr)
            return 1

    ctx = retrieve(args.question, k=args.k, filters=_filters(args),
                   use_vector=not args.no_vector, use_keyword=not args.no_keyword,
                   use_graph=not args.no_graph, graph_hops=args.hops,
                   max_facts=args.max_facts)

    if args.retrieval_only:
        retrieved_sources = synthesize.build_sources(ctx)
        sources = synthesize.select_prompt_sources(retrieved_sources)
        if args.prompt:
            system, user = synthesize.build_prompt(ctx, sources)
            print(system + "\n" + "-" * 70 + "\n" + user)
            return 0
        if args.json:
            print(json.dumps({"context": ctx.to_dict(), "sources": sources,
                              "retrieved_sources": len(retrieved_sources)},
                             ensure_ascii=False, indent=1, default=str))
            return 0
        _print_sources(sources, cited=[], chars=args.chars or 240, show_all=True)
        print(f"\n{json.dumps(ctx.diagnostics, ensure_ascii=False)}", file=sys.stderr)
        return 0

    result = synthesize.answer(
        args.question, k=args.k, ctx=ctx, max_tokens=args.max_tokens)

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=1, default=str))
        return 0

    print(result.text or "(no answer generated)")
    _print_sources(result.sources, result.cited, args.chars, args.all_sources)
    warn = []
    if result.invalid_citations:
        warn.append(f"stripped {len(result.invalid_citations)} citation(s) pointing "
                    f"at no source: {result.invalid_citations}")
    if result.uncited_sentences:
        warn.append(f"{result.uncited_sentences} sentence(s) carry no citation")
    for w in warn:
        print(f"warning: {w}", file=sys.stderr)
    print(json.dumps(result.diagnostics, ensure_ascii=False), file=sys.stderr)
    return 0 if result.text else 1


if __name__ == "__main__":
    raise SystemExit(main())
