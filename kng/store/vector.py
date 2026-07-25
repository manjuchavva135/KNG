"""LanceDB chunk store — the vector half of hybrid retrieval.

Embedded and file-backed (`index/lancedb`), so the index is just a directory that
travels to the query machine. Every provenance field a `Chunk` carries is stored
as a real column, which is what lets retrieval prefilter by date, press meet,
source type or language before ranking — the basis of the cross-meet and
temporal questions this project exists to answer.
"""
from __future__ import annotations

from typing import Any, Iterable

import pyarrow as pa

from ..config import settings
from ..models import Chunk

TABLE = "chunks"

# Columns mirror Chunk's flattened metadata (kng/models.py). Kept explicit rather
# than inferred so a re-embed can never silently change column types.
_BASE_FIELDS = [
    ("chunk_id", pa.string()),
    ("text", pa.string()),
    ("text_original", pa.string()),
    ("text_en", pa.string()),
    ("content_hash", pa.string()),
    ("source_file", pa.string()),
    ("source_type", pa.string()),
    ("press_meet_id", pa.string()),
    ("press_meet_title", pa.string()),
    ("date", pa.string()),
    ("topic", pa.string()),
    ("publication", pa.string()),
    ("speaker", pa.string()),
    ("language", pa.string()),
    ("page", pa.int32()),
    ("slide", pa.int32()),
    ("video_start", pa.float32()),
    ("video_end", pa.float32()),
    ("citation", pa.string()),
]


def schema(dim: int) -> pa.Schema:
    return pa.schema([pa.field("vector", pa.list_(pa.float32(), dim))]
                     + [pa.field(n, t) for n, t in _BASE_FIELDS])


def chunk_to_row(chunk: Chunk, vector: Iterable[float]) -> dict[str, Any]:
    row = chunk.model_dump(exclude={"embedding"})
    row["vector"] = list(vector)
    return row


def connect():
    import lancedb
    path = settings().path(settings().lancedb_path)
    path.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(path))


def open_table(dim: int | None = None, create: bool = False):
    """Open the chunks table, creating it when `create` and a dim is known."""
    db = connect()
    if TABLE in db.table_names():
        return db.open_table(TABLE)
    if not create or dim is None:
        raise FileNotFoundError(
            f"no '{TABLE}' table in {settings().lancedb_path} — run `--stage embed` first")
    return db.create_table(TABLE, schema=schema(dim))


def upsert_file(table, source_file: str, rows: list[dict[str, Any]]) -> int:
    """Replace all rows for one source file — makes re-embedding idempotent."""
    escaped = source_file.replace("'", "''")
    table.delete(f"source_file = '{escaped}'")
    if rows:
        table.add(rows)
    return len(rows)


def ensure_fts(table) -> None:
    """Full-text index over chunk text, for WP4's keyword leg of hybrid search.

    Built here because it belongs with the store; retrofitting it later means
    re-scanning the whole table.
    """
    table.create_fts_index("text", replace=True, use_tantivy=False)


def search(table, vector: Iterable[float], k: int = 8, where: str | None = None) -> list[dict]:
    # Cosine, not the LanceDB default L2. Vectors are normalised so the ranking
    # is identical either way, but `_distance` then reads as 1 - similarity.
    q = table.search(list(vector)).metric("cosine").limit(k)
    if where:
        q = q.where(where, prefilter=True)
    return q.to_list()
