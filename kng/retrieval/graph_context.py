"""Graph retrieval leg — the part passage search cannot do.

"What has he said about the TTD laddu row, and when?" is not one passage. It is
an entity, its neighbourhood, and the set of meets it spans — which is exactly
what `index/graph/` stores. This module turns a natural-language question into
that subgraph and hands the generator citable facts alongside citable passages.

Entity linking is deliberately **lexical, not embedded**: the graph's own names
and alias table are matched against n-grams of the question, longest span first.
An embedding-based linker would silently bind "Lokesh" to the wrong Lokesh, and
misattributing a quote to the wrong politician is the one failure this archive
cannot afford — the same reason WP3's resolution refuses fuzzy matching.

Everything here is free: it reads `index/graph/graph.json` and makes no API call.
It also works before WP3's paid pass finishes, when the graph holds only
structural nodes — it simply links fewer entities and reports that.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from ..graph import ontology as onto
from ..store import graph as gstore

# Question words and archive-generic terms never identify an entity; without
# this "what did he say about power" links the Place node "Say" on a substring.
_STOPWORDS = {
    "what", "who", "when", "where", "why", "how", "did", "does", "do", "is",
    "are", "was", "were", "the", "a", "an", "of", "in", "on", "at", "to", "for",
    "about", "and", "or", "he", "she", "they", "it", "his", "her", "their",
    "said", "say", "says", "tell", "me", "all", "any", "which", "that", "this",
    "press", "meet", "meets", "conference", "news",
}

MAX_NGRAM = 5
# Structural nodes (PressMeet/Source/Publication/Date) match generic words too
# easily and carry no assertion; they are linked only on an exact, longer span.
_MIN_STRUCTURAL_CHARS = 6

_TEMPORAL = re.compile(
    r"\b(when|timeline|over time|chronolog|history|first|latest|since|before|"
    r"after|evolv|progress)\b|ఎప్పుడు|కాలక్రమ", re.I)


def _name_index(G) -> dict[str, list[str]]:
    """normalised surface form → node ids. Built once per loaded graph.

    Cached on `G.graph` so repeated queries in one process (the WP5 server) do
    not rescan every node, while a reloaded graph gets a fresh index.
    """
    idx = G.graph.get("_kng_name_index")
    if idx is not None:
        return idx
    idx = {}
    for nid, d in G.nodes(data=True):
        surfaces = [d.get("name", "")] + list(d.get("aliases", []))
        for s in surfaces:
            key = onto.normalise(s)
            if not key:
                continue
            ids = idx.setdefault(key, [])
            if nid not in ids:
                ids.append(nid)
    # The ontology's alias table maps spellings that may not appear verbatim in
    # the graph ("Jagan" → "Y. S. Jagan Mohan Reddy") onto canonical names.
    for variant, canonical in onto.alias_map().items():
        target = onto.normalise(canonical)
        key = onto.normalise(variant)
        if not key or key == target:
            continue
        for nid in idx.get(target, []):
            idx.setdefault(key, []).append(nid)
    G.graph["_kng_name_index"] = idx
    return idx


def _ngrams(question: str, max_n: int = MAX_NGRAM) -> list[tuple[int, int, str]]:
    """(start, end, normalised text) spans of the question, longest first."""
    words = [w for w in onto.normalise(question).split() if w]
    spans: list[tuple[int, int, str]] = []
    for n in range(min(max_n, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            spans.append((i, i + n, " ".join(words[i:i + n])))
    return spans


def link_entities(G, question: str, limit: int = 6) -> list[dict[str, Any]]:
    """Nodes the question names, longest match first, most-mentioned first.

    A span already covered by a longer match is skipped, so "ys jagan mohan
    reddy" links the person once instead of also linking a "Reddy" node.
    """
    idx = _name_index(G)
    covered: set[int] = set()
    hits: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for start, end, text in _ngrams(question):
        if not text or text in _STOPWORDS:
            continue
        if any(i in covered for i in range(start, end)):
            continue
        node_ids = idx.get(text)
        if not node_ids:
            continue
        matched = False
        for nid in node_ids:
            d = G.nodes[nid]
            if (d.get("type") in onto.STRUCTURAL_TYPES
                    and len(text) < _MIN_STRUCTURAL_CHARS):
                continue
            matched = True
            if nid in seen_nodes:
                continue
            seen_nodes.add(nid)
            hits.append({
                "entity_id": nid, "name": d.get("name", nid),
                "type": d.get("type", ""), "matched": text,
                "mention_count": d.get("mention_count", 0),
                "press_meet_ids": d.get("press_meet_ids", []),
                "first_date": d.get("first_date"), "last_date": d.get("last_date"),
            })
        if matched:
            covered.update(range(start, end))
    hits.sort(key=lambda h: (-len(h["matched"]), -h["mention_count"]))
    return hits[:limit]


def _fact(edge: dict, meet_hint: set[str]) -> dict[str, Any]:
    """One edge rendered as a citable fact."""
    evidence = [
        {"quote": " ".join((ev.get("quote") or "").split()),
         "citation": ev.get("citation", ""), "date": ev.get("date"),
         "chunk_id": ev.get("chunk_id", ""),
         "source_file": ev.get("source_file", ""),
         "press_meet_id": ev.get("press_meet_id", ""),
         "source_type": ev.get("source_type", ""),
         "publication": ev.get("publication"),
         "language": ev.get("language", ""),
         "page": ev.get("page"), "slide": ev.get("slide"),
         "video_start": ev.get("video_start"),
         "video_end": ev.get("video_end")}
        for ev in edge.get("evidence", [])
    ]
    meets = edge.get("press_meet_ids", [])
    return {
        "source": edge["source"], "source_type": edge["source_type"],
        "relation": edge["relation"],
        "target": edge["target"], "target_type": edge["target_type"],
        "weight": edge.get("weight", 1),
        "structural": edge.get("structural", False),
        "first_date": edge.get("first_date"), "last_date": edge.get("last_date"),
        "press_meet_ids": meets,
        "in_retrieved_meet": bool(meet_hint & set(meets)),
        "evidence": evidence,
        "hop": edge.get("hop", 1),
    }


def _filter_active(filters: Any) -> bool:
    if filters is None:
        return False
    return any(getattr(filters, key, None) for key in (
        "language", "source_type", "press_meet_id", "publication",
        "since", "until",
    ))


def _evidence_matches(ev: dict, filters: Any) -> bool:
    """Whether one exact evidence locator satisfies the passage filter scope."""
    if filters is None:
        return True
    for attr, field in (
        ("language", "language"),
        ("source_type", "source_type"),
        ("press_meet_id", "press_meet_id"),
        ("publication", "publication"),
    ):
        wanted = getattr(filters, attr, None)
        if wanted and str(ev.get(field) or "") != str(wanted):
            return False
    date = ev.get("date")
    if getattr(filters, "since", None) and (not date or date < filters.since):
        return False
    if getattr(filters, "until", None) and (not date or date > filters.until):
        return False
    return True


def _scope_fact(fact: dict, filters: Any) -> Optional[dict]:
    """Keep only evidence that the user allowed, or drop the fact.

    An aggregated edge can span eighteen press meets.  Carrying the whole edge
    through a `press_meet_id=10` request makes the filter cosmetic and lets the
    model cite claims from unrelated meets.  Scoping at the evidence record is
    the only exact interpretation because that record owns the date, source type
    and publication.
    """
    matching = [
        ev for ev in fact.get("evidence", [])
        if ev.get("chunk_id") and ev.get("source_file")
        and _evidence_matches(ev, filters)
    ]
    if not matching:
        return None
    # Two exact excerpts are enough to render/cite one relation. Filter before
    # this cap: the relevant meet may be the fifth evidence record on an
    # aggregated cross-meet edge.
    evidence = matching[:2]
    if not _filter_active(filters):
        fact["evidence"] = evidence
        return fact

    scoped = dict(fact)
    scoped["evidence"] = evidence
    meets = sorted({str(ev.get("press_meet_id")) for ev in evidence
                    if ev.get("press_meet_id")})
    dates = sorted({str(ev.get("date")) for ev in evidence if ev.get("date")})
    scoped["press_meet_ids"] = meets
    scoped["first_date"] = dates[0] if dates else None
    scoped["last_date"] = dates[-1] if dates else None
    # The original edge weight covers evidence outside the requested scope.
    # Report only the supporting records actually available to this answer.
    scoped["weight"] = len(matching)
    return scoped


def _terms(text: str) -> set[str]:
    return {t for t in onto.normalise(text).split() if t and t not in _STOPWORDS}


def _fact_text(f: dict) -> str:
    quotes = " ".join(ev.get("quote", "") for ev in f.get("evidence", []))
    return f"{f['source']} {f['relation']} {f['target']} {quotes}"


def _relevance(f: dict, question_terms: set[str]) -> float:
    """How much this edge has to do with the question that was asked.

    A hub entity like Jagan has hundreds of edges; taking its highest-weight
    ones gives an answer about whatever he talks about most, not about what was
    asked. Term overlap with the question is a crude signal, but it is the only
    one available that does not cost an API call, and it works across scripts
    because both sides are folded through `ontology.normalise`.
    """
    if not question_terms:
        return 0.0
    hits = len(question_terms & _terms(_fact_text(f)))
    score = hits / len(question_terms)
    if f["in_retrieved_meet"]:
        # The passages already retrieved are the reader's frame of reference;
        # an edge from the same meet elaborates them rather than changing topic.
        score += 0.5
    if not f["structural"]:
        # "X was published by Sakshi" is provenance the citation already carries;
        # "X ACCUSES Y" is content.
        score += 0.25
    return score


def _fact_rank(f: dict) -> tuple:
    """Order facts by usefulness to an answer, not by graph mechanics."""
    relation_priority = {
        "MAKES_CLAIM": 0,
        "ANNOUNCED_SCHEME": 1,
        "ACCUSES": 2,
        "SUPPORTS": 3,
        "OPPOSES": 3,
        "RESPONDS_TO": 3,
        "MEMBER_OF": 8,
        "MENTIONS": 9,
    }.get(f.get("relation"), 5)
    return (
        f["hop"], -f.get("relevance", 0.0), f["structural"],
        relation_priority, -f["weight"],
    )


def gather(question: str, *, hops: int = 1, max_facts: int = 20,
           passages: Optional[Iterable[dict]] = None,
           filters: Any = None,
           max_timeline: int = 15) -> dict[str, Any]:
    """Entities, facts, timeline and communities for one question.

    Returns empty lists (never raises) when `index/graph/` is absent, so the
    passage-only answer path keeps working on a machine with no graph.
    """
    out: dict[str, Any] = {"entities": [], "facts": [], "communities": [],
                           "timeline": [], "diagnostics": {}}
    try:
        G = gstore.load()
    except FileNotFoundError as e:
        out["diagnostics"]["graph"] = f"unavailable: {e}"
        return out

    meet_hint = {p.get("press_meet_id", "") for p in (passages or []) if p.get("press_meet_id")}
    entities = link_entities(G, question)
    out["entities"] = entities
    out["diagnostics"]["graph_entities_linked"] = len(entities)
    if not entities:
        out["diagnostics"]["graph"] = (
            "no entity in the question matched a graph node "
            f"({G.number_of_nodes()} nodes loaded)")
        return out

    facts: dict[tuple, dict] = {}
    for ent in entities:
        for edge in gstore.neighbors(G, ent["entity_id"], hops=hops):
            key = (edge["source_id"], edge["relation"], edge["target_id"])
            if key not in facts:
                scoped = _scope_fact(_fact(edge, meet_hint), filters)
                if scoped is not None:
                    facts[key] = scoped
    # Terms that linked an entity are removed before scoring: every edge of the
    # Jagan node contains "Jagan", so leaving them in scores all 300 of his
    # edges identically and the topic words in the question decide nothing.
    linked_terms: set[str] = set()
    for e in entities:
        linked_terms |= _terms(e["matched"])
    question_terms = _terms(question) - linked_terms
    for f in facts.values():
        f["relevance"] = round(_relevance(f, question_terms), 4)
    ranked = sorted(facts.values(), key=_fact_rank)
    # An edge that shares neither a topic term with the question nor a meet with
    # the retrieved passages is off-topic hub noise. Drop it — but only when
    # enough on-topic facts remain to answer from, and never when the question
    # was nothing but an entity name, where there is no topic to match on.
    on_topic = [f for f in ranked if f["relevance"] > 0.3]
    if question_terms and len(on_topic) >= 3:
        kept = on_topic[:max_facts]
    elif question_terms:
        # Nothing matched — usually because the question is English and the
        # edge's evidence is Telugu, which term overlap cannot bridge. Keep a
        # few of the strongest as background rather than flooding the prompt
        # with a hub entity's entire neighbourhood.
        kept = ranked[:min(max_facts, 6)]
    else:
        kept = ranked[:max_facts]
    out["facts"] = kept
    out["diagnostics"]["graph_facts"] = (
        f"{len(out['facts'])} of {len(ranked)} "
        f"({len(on_topic)} matched the question)")

    head = entities[0]
    if _TEMPORAL.search(question) or len(head.get("press_meet_ids", [])) > 1:
        rows = gstore.timeline(G, head["entity_id"])
        out["timeline"] = rows[:max_timeline]
        out["diagnostics"]["graph_timeline"] = f"{head['name']}: {len(rows)} meets"

    linked = {e["entity_id"] for e in entities}
    comms = []
    for c in gstore.load_communities():
        overlap = linked & set(c.entity_ids)
        if not overlap:
            continue
        comms.append({
            "community_id": c.community_id, "title": c.title, "summary": c.summary,
            "size": c.size, "press_meet_ids": c.press_meet_ids,
            "members": [G.nodes.get(n, {}).get("name", n) for n in c.entity_ids[:8]],
        })
    comms.sort(key=lambda c: -c["size"])
    out["communities"] = comms[:3]
    if comms and not any(c["summary"] for c in comms):
        # Distinguish "phase E never ran" from "it ran, but these particular
        # clusters are below its min_size" — 577 of 599 communities in this
        # corpus are singletons that earn no paid summary call, so the second
        # case is normal and the first is a missing pipeline step.
        any_summary = any(c.summary for c in gstore.load_communities())
        out["diagnostics"]["communities"] = (
            f"{len(comms)} matched; none summarised (they are below phase E's "
            "min_size)" if any_summary else
            "matched, but phase E has never run — no community is summarised")
    return out
