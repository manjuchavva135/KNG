"""Chunks → knowledge graph (WP3).

Five phases, deliberately split by what they cost:

  A. **structural** — PressMeet / Source / Publication / Date nodes and the edges
     between them, derived from metadata already on disk. Free, deterministic.
  B. **extraction**  — one LLM call per chunk for entities and relations. Paid.
  C. **resolution**  — canonicalise names to one node per real-world thing. Free.
  D. **assembly**    — build the graph and detect Louvain communities. Free.
  E. **summaries**   — one LLM call per community for its "god-node" summary. Paid.

`--structural-only` stops after A+D, which means the whole stage is runnable and
verifiable with no API key and no spend — the standing guardrail on this repo is
that paid passes are the user's to run, so the alternative would be a stage
nobody could test before paying for it.

Phase A always rebuilds from every file: it is cheap, and edges like the
`PRECEDES` chain are properties of the corpus as a whole, not of one file. The
manifest's per-file `graph` status therefore gates only phase B, the paid part,
and the on-disk extraction cache makes even that resumable at chunk granularity.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from ..config import ROOT, settings
from ..graph import ontology as onto
from ..models import Chunk, Entity, Relation
from ..store import graph as gstore

# Publication values are already normalised by the extractor's metadata pass.
_STRUCTURAL = True


def _mk_entity(node_type: str, name: str, *, display: str | None = None,
               meet: str = "", date: str | None = None,
               structural: bool = False, mentions: int = 0) -> Entity:
    return Entity(
        entity_id=onto.entity_id(node_type, name),
        name=display or name,
        type=node_type,
        aliases=[] if (display or name) == name else [name],
        mention_count=mentions,
        press_meet_ids=[meet] if meet else [],
        first_date=date, last_date=date,
        structural=structural,
    )


def _structural_relation(src: str, rel: str, dst: str, *, meet: str = "",
                         date: str | None = None, citation: str = "",
                         source_file: str = "") -> Relation:
    return Relation(source_id=src, relation=rel, target_id=dst,
                    press_meet_id=meet, date=date, citation=citation,
                    source_file=source_file, structural=_STRUCTURAL)


# ── phase A ────────────────────────────────────────────────────────────────────
def build_structural(G, chunks_by_file: dict[str, list[Chunk]]) -> dict:
    """PressMeet / Source / Publication / Date scaffold — no paid calls.

    Everything here is asserted by file metadata rather than inferred from text,
    so it is exact: which meet a document belongs to, when the meet happened,
    which outlet published a clip. The LLM layer then hangs off these anchors.
    """
    counts = {"press_meets": 0, "sources": 0, "publications": 0, "dates": 0,
              "undated_meets": []}
    meets: dict[str, dict] = defaultdict(
        lambda: {"title": "", "dates": set(), "topic": "", "chunks": 0, "files": set()})

    for rel, chunks in chunks_by_file.items():
        if not chunks:
            continue
        head = chunks[0]
        meet_id = head.press_meet_id or "unknown"
        m = meets[meet_id]
        m["title"] = m["title"] or head.press_meet_title or meet_id
        m["topic"] = m["topic"] or head.topic
        m["chunks"] += len(chunks)
        m["files"].add(rel)
        for c in chunks:
            if c.date:
                m["dates"].add(c.date)

    for meet_id, m in meets.items():
        date = min(m["dates"]) if m["dates"] else None
        if date is None:
            counts["undated_meets"].append(meet_id)
        mid = onto.entity_id("PressMeet", meet_id)
        ent = _mk_entity("PressMeet", meet_id, display=m["title"] or meet_id,
                         meet=meet_id, date=date, structural=True)
        gstore.add_entity(G, ent)
        G.nodes[mid]["topic"] = m["topic"]
        G.nodes[mid]["chunk_count"] = m["chunks"]
        G.nodes[mid]["file_count"] = len(m["files"])
        counts["press_meets"] += 1

        if date:
            did = onto.entity_id("Date", date)
            gstore.add_entity(G, _mk_entity("Date", date, meet=meet_id, date=date,
                                            structural=True))
            gstore.add_relation(G, _structural_relation(mid, "HELD_ON", did,
                                                        meet=meet_id, date=date))
            counts["dates"] += 1

    for rel, chunks in chunks_by_file.items():
        if not chunks:
            continue
        head = chunks[0]
        meet_id = head.press_meet_id or "unknown"
        mid = onto.entity_id("PressMeet", meet_id)
        date = min([c.date for c in chunks if c.date], default=None)

        sid = onto.entity_id("Source", rel)
        gstore.add_entity(G, _mk_entity("Source", rel, display=Path(rel).name,
                                        meet=meet_id, date=date, structural=True,
                                        mentions=len(chunks)))
        G.nodes[sid]["source_type"] = head.source_type
        G.nodes[sid]["source_file"] = rel
        gstore.add_relation(G, _structural_relation(
            mid, "CITES_SOURCE", sid, meet=meet_id, date=date,
            citation=head.citation, source_file=rel))
        counts["sources"] += 1

        pub = head.publication
        if pub:
            pid = onto.entity_id("Publication", pub)
            gstore.add_entity(G, _mk_entity("Publication", pub, meet=meet_id,
                                            date=date, structural=True))
            gstore.add_relation(G, _structural_relation(
                mid, "COVERED_BY", pid, meet=meet_id, date=date, source_file=rel))
            counts["publications"] += 1

    counts["publications"] = sum(
        1 for _, d in G.nodes(data=True) if d.get("type") == "Publication")
    _link_precedes(G, meets)
    return counts


def _link_precedes(G, meets: dict[str, dict]) -> int:
    """Chain dated meets in chronological order.

    Undated meets are skipped rather than sorted to the front: three
    `press_meet_id`s in this corpus are filename fallbacks with no date, and
    threading them into the chain would corrupt every timeline that walks it.
    They are reported as a diagnostic by the caller instead.
    """
    dated = sorted(
        ((min(m["dates"]), mid) for mid, m in meets.items() if m["dates"]),
        key=lambda t: t[0])
    made = 0
    for (d1, m1), (d2, m2) in zip(dated, dated[1:]):
        gstore.add_relation(G, _structural_relation(
            onto.entity_id("PressMeet", m1), "PRECEDES",
            onto.entity_id("PressMeet", m2), meet=m1, date=d1))
        made += 1
    return made


# ── phase D ────────────────────────────────────────────────────────────────────
def detect_communities(G, resolution: float | None = None) -> list:
    """Louvain clusters over the undirected projection.

    NetworkX's own implementation, so the artifact needs no compiled extension on
    the query machine. Seeded, because an unseeded run would give the handover a
    different community count every time it was checked.
    """
    from networkx.algorithms.community import louvain_communities

    from ..models import Community

    s = settings()
    U = gstore.to_weighted_undirected(G)
    if U.number_of_edges() == 0:
        return []
    groups = louvain_communities(
        U, weight="weight",
        resolution=resolution if resolution is not None else s.community_resolution,
        seed=s.llm_seed)

    out: list[Community] = []
    for i, members in enumerate(sorted(groups, key=len, reverse=True)):
        members = sorted(members)
        meets: set[str] = set()
        for nid in members:
            meets |= set(G.nodes[nid].get("press_meet_ids", []))
        cid = f"c{i:04d}"
        out.append(Community(community_id=cid, level=0, entity_ids=members,
                             press_meet_ids=sorted(meets), size=len(members)))
        for nid in members:
            G.nodes[nid]["community"] = cid
    return out


# ── stage entry point ──────────────────────────────────────────────────────────
def _load_all_chunks(files: list[Path]) -> dict[str, list[Chunk]]:
    from .chunk import load_chunks
    out: dict[str, list[Chunk]] = {}
    for path in files:
        rel = str(path.relative_to(ROOT))
        chunks = load_chunks(rel)
        if chunks is not None:
            out[rel] = chunks
    return out


def _describe_graph(counts: dict, G=None) -> None:
    """Record cumulative graph size, not just this run's work.

    Called on the no-op path too. WP2 shipped a defect where re-running a
    finished stage overwrote real stats with zeroes; this stage must not repeat
    it.
    """
    counts.update(gstore.describe(G))


def run_graph(man, files: list[Path], *, structural_only: bool = False,
              force: bool = False, concurrency: int | None = None,
              summaries: bool = True, plan_only: bool = False,
              retry_split: int = 0) -> dict:
    """Build the knowledge graph. Free unless `structural_only` is False."""
    import json as _json

    from ..stats import set_stage

    counts: dict = {"total": len(files), "processed": 0, "skipped": 0, "errors": 0,
                    "chunks": 0, "sarvam_calls": {}}
    chunks_by_file = _load_all_chunks(files)
    counts["chunks"] = sum(len(c) for c in chunks_by_file.values())

    if plan_only:
        # Costs nothing and answers the only question worth asking before a paid
        # pass: how many calls, over what, and what got trimmed.
        from .graph_extract import plan_report
        report = plan_report(chunks_by_file, files)
        print(_json.dumps(report, indent=2, ensure_ascii=False))
        return report

    if not chunks_by_file:
        _describe_graph(counts)
        print("graph: no chunks found — run `--stage chunk` first", file=sys.stderr)
        set_stage("graph", counts)
        return counts

    G = gstore.new_graph()
    structural = build_structural(G, chunks_by_file)
    counts.update(structural)
    counts["processed"] = len(chunks_by_file)
    print(f"graph[A] structural: {structural['press_meets']} meets · "
          f"{structural['sources']} sources · {structural['publications']} publications · "
          f"{G.number_of_nodes()} nodes / {G.number_of_edges()} edges", file=sys.stderr)
    if structural["undated_meets"]:
        print(f"  undated meets (excluded from PRECEDES chain): "
              f"{structural['undated_meets']}", file=sys.stderr)

    if not structural_only:
        from .graph_extract import run_extraction_phases
        run_extraction_phases(G, man, files, chunks_by_file, counts,
                              force=force, concurrency=concurrency,
                              summaries=summaries, retry_split=retry_split)

    communities = detect_communities(G)
    counts["community_count"] = len(communities)
    print(f"graph[D] communities: {len(communities)} "
          f"(largest {max((c.size for c in communities), default=0)} entities)",
          file=sys.stderr)

    if not structural_only and summaries and communities:
        from ..providers import get_llm
        from .graph_extract import summarise_communities
        llm = get_llm()
        done = summarise_communities(
            G, communities, llm, concurrency=concurrency or settings().llm_concurrency)
        counts["summaries"] = done
        counts.setdefault("sarvam_calls", {})["summary"] = done
        print(f"graph[E] summaries: {done} communities described", file=sys.stderr)

    gstore.save(G, communities)
    mirrored = gstore.export_neo4j(G)
    if mirrored:
        print(f"graph: mirrored {mirrored} elements into Neo4j", file=sys.stderr)

    _describe_graph(counts, G)
    set_stage("graph", counts)
    print(f"graph: {counts['nodes']} nodes · {counts['edges']} edges · "
          f"{counts['community_count']} communities → {gstore.graph_file()}",
          file=sys.stderr)
    return counts
