"""Resolving a citation back to the text it came from.

A citation the reader cannot open is a claim on trust, which is exactly what this
project refuses to ask for. Clicking `[2]` has to show the passage.

Text comes from `index/chunks/<source_file>.json` (the same records retrieval
ranked) rather than the original PDF, because `/data/` is git-ignored and absent
on a fresh clone — the citation must still resolve there. The original file is
offered only when it happens to exist locally.

Both entry points take a caller-supplied path, so both resolve it and verify
containment before touching the filesystem. `../../etc/passwd` is the obvious
attempt and it is checked for explicitly, but the containment test is what
actually holds — including for symlinks, since `resolve()` follows them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..config import ROOT, settings


class SourceNotFound(Exception):
    """Raised for a missing file *and* for one outside the allowed root.

    Deliberately the same error either way: distinguishing them tells a prober
    which paths exist.
    """


def _safe_under(rel: str, base: Path) -> Path:
    """Resolve `rel` under `base`, or raise if it escapes."""
    if not rel:
        raise SourceNotFound("no path given")
    candidate = (base / rel).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise SourceNotFound("path outside the permitted root")
    return candidate


def chunk_records(source_file: str) -> list[dict[str, Any]]:
    """Every chunk of one source file, from the committed index."""
    fp = _safe_under(source_file + ".json", settings().index_dir / "chunks")
    if not fp.exists():
        raise SourceNotFound(f"no indexed text for {source_file}")
    try:
        records = json.loads(fp.read_text(encoding="utf-8"))
    except ValueError:
        raise SourceNotFound(f"unreadable index entry for {source_file}")
    return records if isinstance(records, list) else []


def passage(source_file: str, *, chunk_id: Optional[str] = None,
            page: Optional[int] = None) -> dict[str, Any]:
    """One passage plus its neighbours, for the source viewer.

    Prefers an exact `chunk_id`, then a page match, then the file's first chunk —
    so a citation without a page (a news clip, a video) still opens somewhere
    useful instead of erroring.
    """
    records = chunk_records(source_file)
    if not records:
        raise SourceNotFound(f"no text indexed for {source_file}")

    hit = None
    if chunk_id:
        hit = next((r for r in records if r.get("chunk_id") == chunk_id), None)
        if hit is None:
            raise SourceNotFound(f"no indexed passage {chunk_id!r} in {source_file}")
    if hit is None and page is not None:
        hit = next((r for r in records if r.get("page") == page), None)
        if hit is None:
            raise SourceNotFound(f"no indexed page {page} in {source_file}")
    if hit is None:
        hit = records[0]

    index = records.index(hit)
    raw_path = settings().path(source_file)
    return {
        "source_file": source_file,
        "citation": hit.get("citation", ""),
        "press_meet_id": hit.get("press_meet_id", ""),
        "press_meet_title": hit.get("press_meet_title", ""),
        "date": hit.get("date"),
        "source_type": hit.get("source_type", ""),
        "publication": hit.get("publication"),
        "language": hit.get("language", ""),
        "page": hit.get("page"),
        "slide": hit.get("slide"),
        "video_start": hit.get("video_start"),
        "text": hit.get("text", ""),
        "chunk_id": hit.get("chunk_id", ""),
        "position": {"index": index, "total": len(records)},
        # Neighbouring passages let the reader see a claim in context rather than
        # as an isolated snippet.
        "neighbours": [
            {"chunk_id": r.get("chunk_id", ""), "page": r.get("page"),
             "citation": r.get("citation", ""),
             "preview": " ".join((r.get("text") or "").split())[:160]}
            for r in records[max(0, index - 2): index + 3]
            if r.get("chunk_id") != hit.get("chunk_id")
        ],
        "raw_available": raw_path.exists(),
    }


def raw_file(source_file: str) -> Path:
    """The original file, when this machine has `/data/`. Never outside it."""
    path = _safe_under(source_file, ROOT)
    data_root = settings().data_dir.resolve()
    if data_root not in path.parents:
        raise SourceNotFound("only files under the data root can be served")
    if not path.is_file():
        raise SourceNotFound(f"{source_file} is not present on this machine")
    return path
