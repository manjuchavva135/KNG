"""NetworkX knowledge-graph store — the graph half of hybrid retrieval.

Embedded and file-backed under `index/graph/`, mirroring `vector.py`: the whole
index is a directory that travels to the query machine, with no server to stand
up. Serialised as **node-link JSON** rather than pickle (portable across Python
and NetworkX versions, and safe to load) or GraphML (which cannot hold the list
and dict attributes every node here carries without flattening them). It is also
plain text, so a graph defect can be found with `grep` instead of a debugger.

Edges are keyed by relation type on a `MultiDiGraph`, so the same pair of
entities can be joined by several relations while repeated assertions of *one*
relation collapse into a single edge whose `weight` counts them — an accusation
repeated across twelve meets should read as stronger than one made in passing,
and its evidence list is what the citation renders from.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config import settings
from ..models import Community, Entity, Relation

GRAPH_FILE = "graph.json"
COMMUNITIES_FILE = "communities.json"

# Evidence is capped per edge: a relation asserted in hundreds of chunks needs a
# few citable examples, not all of them, and the uncapped list would dominate the
# artifact's size. `weight` still records the true count.
MAX_EVIDENCE = 5


def graph_dir() -> Path:
    return settings().path(settings().graph_path)


def graph_file() -> Path:
    return graph_dir() / GRAPH_FILE


# ── construction ───────────────────────────────────────────────────────────────
def new_graph():
    import networkx as nx
    return nx.MultiDiGraph()


def add_entity(G, ent: Entity) -> None:
    """Insert or merge an entity node. Merging is additive and idempotent."""
    node = G.nodes.get(ent.entity_id)
    if node is None:
        G.add_node(
            ent.entity_id, name=ent.name, type=ent.type,
            aliases=sorted(set(ent.aliases)), mention_count=ent.mention_count,
            press_meet_ids=sorted(set(ent.press_meet_ids)),
            first_date=ent.first_date, last_date=ent.last_date,
            structural=ent.structural,
        )
        return
    node["aliases"] = sorted(set(node.get("aliases", [])) | set(ent.aliases))
    node["mention_count"] = node.get("mention_count", 0) + ent.mention_count
    node["press_meet_ids"] = sorted(
        set(node.get("press_meet_ids", [])) | set(ent.press_meet_ids))
    node["first_date"] = _min_date(node.get("first_date"), ent.first_date)
    node["last_date"] = _max_date(node.get("last_date"), ent.last_date)
    node["structural"] = node.get("structural", False) or ent.structural


def add_relation(G, rel: Relation) -> None:
    """Insert an edge, or reinforce it if this relation was already asserted."""
    if rel.source_id not in G or rel.target_id not in G:
        return
    key = rel.relation
    data = G.get_edge_data(rel.source_id, rel.target_id, key=key)
    ev = {"chunk_id": rel.chunk_id, "citation": rel.citation,
          "quote": rel.evidence, "date": rel.date,
          "press_meet_id": rel.press_meet_id}
    if data is None:
        G.add_edge(rel.source_id, rel.target_id, key=key, relation=key, weight=1,
                   structural=rel.structural,
                   press_meet_ids=[rel.press_meet_id] if rel.press_meet_id else [],
                   first_date=rel.date, last_date=rel.date,
                   evidence=[ev] if rel.chunk_id else [])
        return
    data["weight"] = data.get("weight", 0) + 1
    data["first_date"] = _min_date(data.get("first_date"), rel.date)
    data["last_date"] = _max_date(data.get("last_date"), rel.date)
    if rel.press_meet_id:
        data["press_meet_ids"] = sorted(
            set(data.get("press_meet_ids", [])) | {rel.press_meet_id})
    if rel.chunk_id and len(data.get("evidence", [])) < MAX_EVIDENCE:
        data.setdefault("evidence", []).append(ev)


def _min_date(a: Optional[str], b: Optional[str]) -> Optional[str]:
    return min([d for d in (a, b) if d], default=None)


def _max_date(a: Optional[str], b: Optional[str]) -> Optional[str]:
    return max([d for d in (a, b) if d], default=None)


# ── persistence ────────────────────────────────────────────────────────────────
def save(G, communities: Optional[Iterable[Community]] = None) -> Path:
    """Write the graph (and communities) under `index/graph/`."""
    import networkx as nx
    d = graph_dir()
    d.mkdir(parents=True, exist_ok=True)
    # `edges=` is passed explicitly: NetworkX 3.6 warns on the default and the
    # key name is what a reader must pass back, so pinning it keeps the artifact
    # loadable by future versions.
    payload = nx.node_link_data(G, edges="edges")
    (d / GRAPH_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if communities is not None:
        (d / COMMUNITIES_FILE).write_text(
            json.dumps([c.model_dump() for c in communities],
                       ensure_ascii=False, indent=1), encoding="utf-8")
    return d / GRAPH_FILE


def load():
    """Load the graph, or raise FileNotFoundError with the command that builds it."""
    import networkx as nx
    fp = graph_file()
    if not fp.exists():
        raise FileNotFoundError(
            f"no graph at {fp} — run `python -m kng.pipeline.run --stage graph` first")
    data = json.loads(fp.read_text(encoding="utf-8"))
    return nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")


def load_communities() -> list[Community]:
    fp = graph_dir() / COMMUNITIES_FILE
    if not fp.exists():
        return []
    return [Community(**c) for c in json.loads(fp.read_text(encoding="utf-8"))]


def describe(G=None) -> dict[str, Any]:
    """Cumulative graph size, for stats.json and the handover's counts table."""
    try:
        G = G if G is not None else load()
    except FileNotFoundError:
        return {"nodes": 0, "edges": 0, "by_node_type": {}, "by_relation": {}}
    by_type: dict[str, int] = {}
    for _, d in G.nodes(data=True):
        t = d.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    by_rel: dict[str, int] = {}
    for _, _, d in G.edges(data=True):
        r = d.get("relation", "?")
        by_rel[r] = by_rel.get(r, 0) + 1
    return {"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
            "by_node_type": dict(sorted(by_type.items())),
            "by_relation": dict(sorted(by_rel.items())),
            "communities": len(load_communities())}


# ── queries (the seam WP4's graph retrieval leg plugs into) ────────────────────
def find_entities(G, query: str, type: str | None = None,
                  limit: int = 10) -> list[tuple[str, dict]]:
    """Resolve a user string to nodes: exact id, then canonical alias, then
    substring over every observed surface form.

    Substring is last because it is the loosest: "Jagan" should reach the
    canonical person via the alias table, not by accidentally matching a
    press-meet title that contains the word.
    """
    from ..graph.ontology import canonical_name, entity_id, normalise

    if query in G:
        return [(query, G.nodes[query])]
    hits: list[tuple[str, dict]] = []
    if type:
        nid = entity_id(type, query)
        if nid in G:
            hits.append((nid, G.nodes[nid]))
    else:
        for t in {d.get("type") for _, d in G.nodes(data=True)}:
            nid = entity_id(t, query) if t else None
            if nid and nid in G:
                hits.append((nid, G.nodes[nid]))
    if hits:
        return hits[:limit]

    needle = normalise(canonical_name(query))
    if not needle:
        return []
    for nid, d in G.nodes(data=True):
        if type and d.get("type") != type:
            continue
        names = [d.get("name", "")] + list(d.get("aliases", []))
        if any(needle in normalise(n) for n in names if n):
            hits.append((nid, d))
    hits.sort(key=lambda x: -x[1].get("mention_count", 0))
    return hits[:limit]


def neighbors(G, entity_id: str, hops: int = 1,
              relations: set[str] | None = None) -> list[dict[str, Any]]:
    """Edges within `hops` of a node, following both directions.

    Direction-agnostic on purpose: "who is connected to SECI" must surface both
    `Jagan -ACCUSES-> SECI` and `SECI -LOCATED_IN-> Delhi`.
    """
    if entity_id not in G:
        return []
    seen = {entity_id}
    frontier = {entity_id}
    out: list[dict[str, Any]] = []
    for hop in range(1, hops + 1):
        nxt: set[str] = set()
        for node in frontier:
            for u, v, k, d in list(G.out_edges(node, keys=True, data=True)) + \
                              list(G.in_edges(node, keys=True, data=True)):
                if relations and k not in relations:
                    continue
                out.append(_edge_record(G, u, v, k, d, hop))
                for other in (u, v):
                    if other not in seen:
                        nxt.add(other)
        seen |= nxt
        frontier = nxt
        if not frontier:
            break
    # An undirected walk visits each edge from both endpoints; keep one copy.
    uniq: dict[tuple, dict] = {}
    for rec in out:
        uniq.setdefault((rec["source_id"], rec["relation"], rec["target_id"]), rec)
    return sorted(uniq.values(), key=lambda r: (r["hop"], -r["weight"]))


def _edge_record(G, u: str, v: str, k: str, d: dict, hop: int = 0) -> dict[str, Any]:
    return {
        "source_id": u, "source": G.nodes[u].get("name", u),
        "source_type": G.nodes[u].get("type", ""),
        "relation": k,
        "target_id": v, "target": G.nodes[v].get("name", v),
        "target_type": G.nodes[v].get("type", ""),
        "weight": d.get("weight", 1),
        "structural": d.get("structural", False),
        "press_meet_ids": d.get("press_meet_ids", []),
        "first_date": d.get("first_date"), "last_date": d.get("last_date"),
        "evidence": d.get("evidence", []),
        "hop": hop,
    }


def paths(G, a: str, b: str, cutoff: int = 4, limit: int = 5) -> list[list[dict]]:
    """Shortest connecting paths, computed on the undirected projection.

    "How do SECI and the CAG report connect" is a question about connection, not
    about edge direction, so the search ignores it and the rendered path restores
    each hop's real orientation.
    """
    import networkx as nx
    if a not in G or b not in G:
        return []
    U = nx.Graph(G)
    try:
        gen = nx.shortest_simple_paths(U, a, b)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []
    found: list[list[dict]] = []
    for nodes in gen:
        if len(nodes) - 1 > cutoff:
            break
        hops: list[dict] = []
        for x, y in zip(nodes, nodes[1:]):
            data = G.get_edge_data(x, y) or {}
            if data:
                k, d = max(data.items(), key=lambda kv: kv[1].get("weight", 1))
                hops.append(_edge_record(G, x, y, k, d))
                continue
            data = G.get_edge_data(y, x) or {}
            k, d = max(data.items(), key=lambda kv: kv[1].get("weight", 1))
            hops.append(_edge_record(G, y, x, k, d))
        found.append(hops)
        if len(found) >= limit:
            break
    return found


def timeline(G, entity_id: str) -> list[dict[str, Any]]:
    """Every press meet this entity appears in, in date order.

    This is the cross-meet/temporal query the project exists for: an Issue node's
    timeline is the thread of that controversy across the archive.
    """
    if entity_id not in G:
        return []
    meets = set(G.nodes[entity_id].get("press_meet_ids", []))
    for _, _, d in G.out_edges(entity_id, data=True):
        meets |= set(d.get("press_meet_ids", []))
    for _, _, d in G.in_edges(entity_id, data=True):
        meets |= set(d.get("press_meet_ids", []))

    from ..graph.ontology import entity_id as _eid

    rows = []
    for meet in meets:
        node = G.nodes.get(_eid("PressMeet", meet), {})
        rows.append({"press_meet_id": meet,
                     "date": node.get("first_date"),
                     "title": node.get("name", meet)})
    # Undated meets sort last rather than crashing the comparison: three
    # press_meet_ids in this corpus are filename fallbacks with no date at all.
    return sorted(rows, key=lambda r: (r["date"] is None, r["date"] or "", r["press_meet_id"]))


def to_weighted_undirected(G):
    """Undirected simple projection used for community detection.

    Louvain needs one weighted undirected edge per pair; parallel typed edges are
    summed so a pair joined by several strongly-attested relations clusters more
    tightly than a pair joined by one weak mention.
    """
    import networkx as nx
    U = nx.Graph()
    U.add_nodes_from(G.nodes())
    for u, v, d in G.edges(data=True):
        if u == v:
            continue
        w = d.get("weight", 1)
        if U.has_edge(u, v):
            U[u][v]["weight"] += w
        else:
            U.add_edge(u, v, weight=w)
    return U


# ── optional Neo4j mirror ──────────────────────────────────────────────────────
def export_neo4j(G) -> int:
    """Mirror the graph into Neo4j when GRAPH_BACKEND=neo4j and a URI is set.

    Kept optional and off the default path: the portability requirement means the
    NetworkX artifact is authoritative and Neo4j is a convenience for whoever
    wants Cypher on the target system.
    """
    s = settings()
    if s.graph_backend != "neo4j" or not s.neo4j_uri:
        return 0
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    written = 0
    with driver.session() as sess:
        sess.run("MATCH (n:KngEntity) DETACH DELETE n")
        for nid, d in G.nodes(data=True):
            sess.run(
                "MERGE (n:KngEntity {id:$id}) SET n.name=$name, n.type=$type, "
                "n.press_meet_ids=$meets, n.mention_count=$mc",
                id=nid, name=d.get("name", ""), type=d.get("type", ""),
                meets=d.get("press_meet_ids", []), mc=d.get("mention_count", 0))
            written += 1
        for u, v, k, d in G.edges(keys=True, data=True):
            sess.run(
                "MATCH (a:KngEntity {id:$u}), (b:KngEntity {id:$v}) "
                "MERGE (a)-[r:REL {type:$k}]->(b) SET r.weight=$w, r.meets=$meets",
                u=u, v=v, k=k, w=d.get("weight", 1),
                meets=d.get("press_meet_ids", []))
            written += 1
    driver.close()
    return written
