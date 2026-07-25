"""Chunk → vector → LanceDB.

The only expensive stage that costs nothing: embeddings run on a local
multilingual model (`kng/providers/embeddings.py`), so this is reproducible on
the target machine and needs no API key. Work is committed per source file and
tracked in the manifest, so a killed run resumes instead of restarting.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..config import ROOT
from ..store import vector as vstore

# Chunks are ~1000 tokens; a modest batch keeps peak memory sane on CPU while
# still giving the model enough work per call to stay efficient.
BATCH = 16


def _describe_index(counts: dict) -> None:
    """Record the cumulative state of the vector store, not just this run's work."""
    try:
        table = vstore.open_table()
    except FileNotFoundError:
        counts["rows"] = 0
        return
    counts["rows"] = table.count_rows()
    by_type: dict[str, int] = {}
    for st in table.to_pandas()["source_type"]:
        by_type[st] = by_type.get(st, 0) + 1
    counts["indexed_by_type"] = by_type


def run_embed(man, files: list[Path]) -> dict:
    """Embed every chunked doc that needs it and upsert into LanceDB."""
    from ..providers import get_embedder
    from ..stats import set_stage
    from .chunk import load_chunks

    todo = [p for p in files if man.needs(str(p.relative_to(ROOT)), p, "embed")]
    counts: dict = {"total": len(files), "processed": 0, "skipped": len(files) - len(todo),
                    "errors": 0, "chunks": 0, "by_type": {}, "dim": 0}
    if not todo:
        # A no-op run must still describe the index it found, or re-running the
        # stage would overwrite real counts with an empty record.
        _describe_index(counts)
        print(f"embed: nothing to do · {counts['rows']} rows already indexed", file=sys.stderr)
        set_stage("embed", counts)
        return counts

    embedder = get_embedder()            # loads the model once (slow first call)
    dim = embedder.dim
    counts["dim"] = dim
    table = vstore.open_table(dim=dim, create=True)

    for i, path in enumerate(todo, start=1):
        rel = str(path.relative_to(ROOT))
        chunks = load_chunks(rel)
        if chunks is None:
            counts["errors"] += 1
            continue
        rows = []
        if chunks:
            texts = [c.text for c in chunks]
            vecs = []
            for s in range(0, len(texts), BATCH):
                vecs.extend(embedder.embed(texts[s:s + BATCH], kind="passage"))
            rows = [vstore.chunk_to_row(c, v) for c, v in zip(chunks, vecs)]
        vstore.upsert_file(table, rel, rows)
        man.mark(rel, "embed", "done")
        counts["processed"] += 1
        counts["chunks"] += len(rows)
        for c in chunks:
            counts["by_type"][c.source_type] = counts["by_type"].get(c.source_type, 0) + 1
        if i % 25 == 0:
            man.save()
            print(f"  [{i}/{len(todo)}] {counts['chunks']} chunks embedded", file=sys.stderr)

    man.save()
    vstore.ensure_fts(table)             # keyword leg for WP4 hybrid retrieval
    # `processed`/`skipped` are per-run by convention, so a resumed run would
    # otherwise report only its own tail. Record the whole index too.
    _describe_index(counts)
    set_stage("embed", counts)
    print(f"embed: {counts['processed']}/{counts['total']} docs · "
          f"{counts['chunks']} chunks · dim {dim} · "
          f"{table.count_rows()} rows in lancedb", file=sys.stderr)
    return counts
