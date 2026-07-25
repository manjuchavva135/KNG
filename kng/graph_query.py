"""Graph smoke test — the free counterpart to `kng.query`'s vector retrieval.

    python -m kng.graph_query stats
    python -m kng.graph_query entities --type Person --top 20
    python -m kng.graph_query neighbors "YS Jagan" --hops 2
    python -m kng.graph_query path "YS Jagan" "SECI"
    python -m kng.graph_query timeline "TTD Laddu"
    python -m kng.graph_query communities --top 10

Retrieval and inspection only — grounded synthesis over these results is WP4.
Every command reads `index/graph/` and costs nothing, which is what lets the
graph be verified before any paid pass runs.
"""
from __future__ import annotations

import argparse
import sys

from .store import graph as gstore


def _resolve(G, name: str, type_: str | None = None) -> tuple[str, dict] | None:
    hits = gstore.find_entities(G, name, type=type_, limit=5)
    if not hits:
        print(f"no entity matching {name!r}"
              + (f" of type {type_}" if type_ else ""), file=sys.stderr)
        return None
    if len(hits) > 1:
        alts = ", ".join(f"{d.get('name')} [{d.get('type')}]" for _, d in hits[1:])
        print(f"matched {hits[0][1].get('name')} "
              f"[{hits[0][1].get('type')}]; also matched: {alts}", file=sys.stderr)
    return hits[0]


def _fmt_edge(e: dict, show_evidence: bool = False) -> str:
    span = ""
    if e.get("first_date"):
        span = f"  {e['first_date']}" + (
            f"→{e['last_date']}" if e.get("last_date") != e.get("first_date") else "")
    tag = "" if e.get("structural") else " *"
    line = (f"{e['source']} [{e['source_type']}]  --{e['relation']}{tag}-->  "
            f"{e['target']} [{e['target_type']}]  ×{e['weight']}{span}")
    if show_evidence:
        for ev in e.get("evidence", [])[:2]:
            quote = " ".join((ev.get("quote") or "").split())[:120]
            if quote or ev.get("citation"):
                line += f"\n        “{quote}”  — {ev.get('citation', '')}"
    return line


def cmd_stats(G, args) -> int:
    import json
    print(json.dumps(gstore.describe(G), indent=2, ensure_ascii=False))
    return 0


def cmd_entities(G, args) -> int:
    rows = [(nid, d) for nid, d in G.nodes(data=True)
            if not args.type or d.get("type") == args.type]
    rows.sort(key=lambda x: (-x[1].get("mention_count", 0), x[1].get("name", "")))
    for nid, d in rows[: args.top]:
        meets = len(d.get("press_meet_ids", []))
        aliases = ", ".join(d.get("aliases", [])[:3])
        print(f"{d.get('mention_count', 0):>5}  {d.get('type', ''):<13} "
              f"{d.get('name', nid)[:50]:<50} {meets:>3} meets"
              + (f"  ({aliases})" if aliases else ""))
    print(f"\n{len(rows)} entities" + (f" of type {args.type}" if args.type else ""),
          file=sys.stderr)
    return 0


def cmd_neighbors(G, args) -> int:
    hit = _resolve(G, args.entity, args.type)
    if hit is None:
        return 1
    nid, d = hit
    rels = set(args.rel.split(",")) if args.rel else None
    edges = gstore.neighbors(G, nid, hops=args.hops, relations=rels)
    print(f"\n{d.get('name')} [{d.get('type')}] — {d.get('mention_count', 0)} mentions, "
          f"{len(d.get('press_meet_ids', []))} meets\n")
    for e in edges[: args.top]:
        print("  " + _fmt_edge(e, show_evidence=args.evidence))
    print(f"\n{len(edges)} edges within {args.hops} hop(s)", file=sys.stderr)
    return 0


def cmd_path(G, args) -> int:
    a = _resolve(G, args.source)
    b = _resolve(G, args.target)
    if a is None or b is None:
        return 1
    routes = gstore.paths(G, a[0], b[0], cutoff=args.cutoff, limit=args.top)
    if not routes:
        print(f"no path within {args.cutoff} hops", file=sys.stderr)
        return 0
    for i, hops in enumerate(routes, start=1):
        print(f"\n[{i}] {len(hops)} hop(s)")
        for e in hops:
            print("  " + _fmt_edge(e, show_evidence=args.evidence))
    return 0


def cmd_timeline(G, args) -> int:
    hit = _resolve(G, args.entity, args.type)
    if hit is None:
        return 1
    nid, d = hit
    rows = gstore.timeline(G, nid)
    print(f"\n{d.get('name')} [{d.get('type')}] across {len(rows)} press meet(s)\n")
    for r in rows:
        print(f"  {r['date'] or '(undated)':<12} meet {r['press_meet_id']:<8} {r['title'][:60]}")
    return 0


def cmd_communities(G, args) -> int:
    comms = gstore.load_communities()
    if not comms:
        print("no communities recorded — run `--stage graph`", file=sys.stderr)
        return 1
    if args.id:
        sel = [c for c in comms if c.community_id == args.id]
        if not sel:
            print(f"no community {args.id}", file=sys.stderr)
            return 1
        c = sel[0]
        print(f"{c.community_id}  {c.title or '(no summary yet)'}  "
              f"— {c.size} entities, {len(c.press_meet_ids)} meets")
        if c.summary:
            print(f"\n{c.summary}\n")
        for nid in c.entity_ids[: args.top]:
            d = G.nodes.get(nid, {})
            print(f"  {d.get('type', ''):<13} {d.get('name', nid)[:60]}")
        return 0
    for c in comms[: args.top]:
        head = ", ".join(G.nodes.get(n, {}).get("name", n) for n in c.entity_ids[:5])
        print(f"{c.community_id}  {c.size:>4} entities  {len(c.press_meet_ids):>3} meets  "
              f"{c.title or head[:70]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.graph_query")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="node/edge counts by type")

    p = sub.add_parser("entities", help="most-mentioned entities")
    p.add_argument("--type", help="Person | Party | Organization | Place | Scheme | Issue | …")
    p.add_argument("--top", type=int, default=25)

    p = sub.add_parser("neighbors", help="edges around an entity")
    p.add_argument("entity")
    p.add_argument("--type")
    p.add_argument("--hops", type=int, default=1)
    p.add_argument("--rel", help="comma-separated relation filter")
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--evidence", action="store_true", help="show supporting quotes")

    p = sub.add_parser("path", help="how two entities connect")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--cutoff", type=int, default=4)
    p.add_argument("--top", type=int, default=3)
    p.add_argument("--evidence", action="store_true")

    p = sub.add_parser("timeline", help="press meets an entity spans, in date order")
    p.add_argument("entity")
    p.add_argument("--type")

    p = sub.add_parser("communities", help="topic clusters and their summaries")
    p.add_argument("--id", help="show one community in full")
    p.add_argument("--top", type=int, default=20)

    args = ap.parse_args(argv)
    try:
        G = gstore.load()
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    return {
        "stats": cmd_stats, "entities": cmd_entities, "neighbors": cmd_neighbors,
        "path": cmd_path, "timeline": cmd_timeline, "communities": cmd_communities,
    }[args.cmd](G, args)


if __name__ == "__main__":
    raise SystemExit(main())
