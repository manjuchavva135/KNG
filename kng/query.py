"""Retrieval smoke test — proves plain RAG works end-to-end before WP3/WP4.

    python -m kng.query "ఏపీ మద్యం కుంభకోణం"
    python -m kng.query "Tirupati laddu" -k 5 --lang te --since 2024-09-01

Prints the ranked passages with the citation each one resolves to. This is
retrieval only: grounded answer synthesis is WP4.
"""
from __future__ import annotations

import argparse
import sys

from .store import vector as vstore


def _where(args) -> str | None:
    """SQL prefilter — the point of storing provenance as real columns."""
    clauses = []
    if args.lang:
        clauses.append(f"language = '{args.lang}'")
    if args.source_type:
        clauses.append(f"source_type = '{args.source_type}'")
    if args.meet:
        clauses.append(f"press_meet_id = '{args.meet}'")
    if args.since:
        clauses.append(f"date >= '{args.since}'")
    if args.until:
        clauses.append(f"date <= '{args.until}'")
    return " AND ".join(clauses) or None


def search(question: str, k: int = 8, where: str | None = None,
           dedup: bool = True) -> list[dict]:
    from .providers import get_embedder
    qv = get_embedder().embed_one(question, kind="query")
    # Over-fetch so that collapsing duplicate passages still fills k slots.
    rows = vstore.search(vstore.open_table(), qv, k=k * 3 if dedup else k, where=where)
    if not dedup:
        return rows[:k]
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        h = r.get("content_hash") or r["chunk_id"]
        if h in seen:                    # same passage under a duplicate source file
            continue
        seen.add(h)
        out.append(r)
        if len(out) >= k:
            break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.query")
    ap.add_argument("question")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--lang", help="te | en | hi | mixed")
    ap.add_argument("--source-type", help="press_release | source_doc | news_clip | video | slide | table")
    ap.add_argument("--meet", help="press_meet_id")
    ap.add_argument("--since", help="ISO date lower bound")
    ap.add_argument("--until", help="ISO date upper bound")
    ap.add_argument("--no-dedup", action="store_true",
                    help="keep duplicate passages from byte-identical source files")
    ap.add_argument("--chars", type=int, default=240)
    args = ap.parse_args(argv)

    try:
        rows = search(args.question, k=args.k, where=_where(args), dedup=not args.no_dedup)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    if not rows:
        print("no matches", file=sys.stderr)
        return 0
    for i, r in enumerate(rows, start=1):
        dist = r.get("_distance")
        score = f"{1 - dist:.3f}" if dist is not None else "?"
        snippet = " ".join(r["text"].split())[: args.chars]
        print(f"\n[{i}] score={score}  lang={r['language']}  {r['citation']}")
        print(f"    {snippet}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
