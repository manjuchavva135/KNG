"""Corpus facts the filter sidebar is built from — computed once, then cached.

The UI needs to offer only filters that can actually match something: the press
meets that exist, the source types present, the real date range. Inventing
options the corpus cannot satisfy produces silent empty results, which reads as a
broken app rather than an empty filter.

Scanning `index/chunks/` takes a second or two over 4267 records, so it happens
at startup and is held in memory.
"""
from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from typing import Any

from ..config import settings
from ..store import graph as gstore


@lru_cache(maxsize=1)
def corpus_meta() -> dict[str, Any]:
    """Filter options plus coverage, as the UI consumes them."""
    root = settings().index_dir / "chunks"
    dates: list[str] = []
    source_types: Counter = Counter()
    publications: Counter = Counter()
    languages: Counter = Counter()
    meets: dict[str, dict[str, Any]] = {}
    chunks = 0

    for fp in root.rglob("*.json"):
        try:
            records = json.loads(fp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(records, list):
            continue
        for r in records:
            chunks += 1
            date = r.get("date")
            if date:
                dates.append(date)
            if r.get("source_type"):
                source_types[r["source_type"]] += 1
            if r.get("publication"):
                publications[r["publication"]] += 1
            if r.get("language"):
                languages[r["language"]] += 1
            meet = r.get("press_meet_id")
            if meet and meet not in meets:
                meets[meet] = {"id": meet, "date": date,
                               "title": r.get("press_meet_title") or meet}

    dates.sort()
    # Undated meets sort last rather than crashing the comparison — three
    # press_meet_ids in this corpus are filename fallbacks with no date at all.
    meet_list = sorted(meets.values(),
                       key=lambda m: (m["date"] is None, m["date"] or "", m["id"]))
    try:
        graph = gstore.describe()
    except Exception:                        # a clone without index/graph
        graph = {"nodes": 0, "edges": 0, "communities": 0}

    return {
        "app": "PressMeets RAG",
        "chunks": chunks,
        "dated_chunks": len(dates),
        "coverage": {"start": dates[0] if dates else None,
                     "end": dates[-1] if dates else None},
        "press_meets": meet_list,
        "source_types": [{"value": k, "count": v} for k, v in source_types.most_common()],
        "publications": [{"value": k, "count": v} for k, v in publications.most_common()],
        "languages": [{"value": k, "count": v} for k, v in languages.most_common()],
        "graph": {"nodes": graph.get("nodes", 0), "edges": graph.get("edges", 0),
                  "communities": graph.get("communities", 0)},
    }


def refresh() -> dict[str, Any]:
    corpus_meta.cache_clear()
    return corpus_meta()
