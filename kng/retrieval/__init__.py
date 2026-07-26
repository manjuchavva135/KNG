"""WP4 retrieval — assembles the evidence a grounded answer is written from.

Three legs feed one context:

* **vector** (`hybrid.vector_leg`) — bge-m3 over LanceDB. Cross-lingual, so an
  English question reaches Telugu passages, which is the whole reason the
  archive is embedded rather than only indexed by keyword.
* **keyword** (`hybrid.keyword_leg`) — BM25 over the same table. Names, scheme
  titles and numbers ("SECI", "₹3,000 crore") are exactly where dense retrieval
  is weakest and lexical match is strongest.
* **graph** (`graph_context`) — entities the question names, their neighbourhood
  and their timeline. This is what answers cross-meet and temporal questions
  that no single passage contains.

The two passage legs are fused with Reciprocal Rank Fusion and the graph leg is
carried alongside them, because a fact and a passage are not comparable scores —
they are different kinds of evidence and the prompt presents them as such.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from . import graph_context as gctx
from . import hybrid


@dataclass
class Context:
    """Everything the generator is allowed to write from.

    `passages` and `facts` are numbered together in `sources` so a single `[n]`
    citation scheme covers both; the answer must not cite anything else.
    """
    question: str
    passages: list[dict] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    communities: list[dict] = field(default_factory=list)
    timeline: list[dict] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.passages and not self.facts

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def retrieve(question: str, k: int = 8, *,
             filters: Optional[hybrid.Filters] = None,
             use_vector: bool = True, use_keyword: bool = True,
             use_graph: bool = True, graph_hops: int = 1,
             max_facts: int = 20) -> Context:
    """Run every enabled leg and return the merged evidence.

    A leg that cannot run (no FTS index, no graph file, no embedding model) is
    reported in `diagnostics` and skipped rather than raising: a partial answer
    with honest provenance beats no answer, and the graph is *expected* to be
    partial until WP3's paid pass finishes.
    """
    filters = filters or hybrid.Filters()
    diagnostics: dict[str, Any] = {}

    passages, pdiag = hybrid.search_passages(
        question, k=k, filters=filters,
        use_vector=use_vector, use_keyword=use_keyword)
    diagnostics.update(pdiag)

    ctx = Context(question=question, passages=passages, diagnostics=diagnostics)
    if not use_graph:
        diagnostics["graph"] = "disabled"
        return ctx

    g = gctx.gather(question, hops=graph_hops, max_facts=max_facts,
                    passages=passages)
    ctx.facts = g["facts"]
    ctx.entities = g["entities"]
    ctx.communities = g["communities"]
    ctx.timeline = g["timeline"]
    diagnostics.update(g["diagnostics"])
    return ctx


__all__ = ["Context", "retrieve", "hybrid", "gctx"]
