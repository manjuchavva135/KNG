"""Per-stage document counters, persisted to `index/stats.json`.

WP1b requirement: always be able to report *how many documents were processed*
at each pipeline stage / work package. Each stage writes its counts here via
`set_stage`; `python -m kng.stats` renders a human-readable rollup so the
handover doc can quote exact per-stage document counts.

Counts recorded per stage:
    total       source files considered
    processed   files (re)processed this run
    skipped     files up-to-date in the manifest (not reprocessed)
    errors      files whose extractor raised
    segments    logical text units produced
    by_type     {source_type: count} of processed files
    sarvam_calls {ocr, cleanup, asr, translate: count} paid Sarvam calls made
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .config import ROOT

STATS_PATH = ROOT / "index" / "stats.json"

# Billed call kinds. `graph` (entity/relation extraction) and `summary`
# (community god-nodes) are WP3's paid passes; anything else in the dict is a
# non-billed diagnostic and renders on the "issues" line.
_CALL_KINDS = ("ocr", "cleanup", "asr", "translate", "graph", "summary")


def _load(path: Path = STATS_PATH) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def set_stage(stage: str, counts: dict, path: Path = STATS_PATH) -> dict:
    """Record (overwrite) the counts for one stage and persist to disk."""
    data = _load(path)
    data[stage] = counts
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def render(path: Path = STATS_PATH) -> str:
    data = _load(path)
    if not data:
        return "no stats recorded yet — run a pipeline stage first."
    lines: list[str] = [f"KNG pipeline document counts  ({path})", ""]
    for stage, c in data.items():
        total = c.get("total", 0)
        proc = c.get("processed", 0)
        skip = c.get("skipped", 0)
        err = c.get("errors", 0)
        seg = c.get("segments", 0)
        unit = f"{c['chunks']} chunks" if "chunks" in c else f"{seg} segments"
        lines.append(
            f"{stage:<10} {total:>5} files → {proc} processed · "
            f"{skip} skipped · {err} errors · {unit}"
        )
        if "rows" in c:      # cumulative index size, independent of resumes
            lines.append(f"{'':<10} indexed: {c['rows']} rows in the vector store")
        if "nodes" in c:     # graph size, likewise cumulative
            lines.append(f"{'':<10} graph:   {c['nodes']} nodes · {c['edges']} edges · "
                         f"{c.get('communities', 0)} communities")
            for label, key in (("by node", "by_node_type"), ("by rel ", "by_relation")):
                d = c.get(key) or {}
                if d:
                    bits = " ".join(f"{k}={v}" for k, v in d.items())
                    lines.append(f"{'':<10} {label}: {bits}")
        # a no-op run has no per-run by_type; fall back to the whole index's
        by_type = c.get("by_type") or c.get("indexed_by_type") or {}
        if by_type:
            bits = " ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
            lines.append(f"{'':<10} by_type: {bits}")
        sc = c.get("sarvam_calls") or {}
        billed = sum(v for k, v in sc.items() if k in _CALL_KINDS)
        if sc:
            bits = " ".join(f"{k}={sc.get(k, 0)}" for k in _CALL_KINDS)
            lines.append(f"{'':<10} sarvam:  {bits}  (total {billed})")
            # surface non-billed diagnostics (cleanup_failed, cleanup_lossy, …)
            extra = {k: v for k, v in sorted(sc.items()) if k not in _CALL_KINDS}
            if extra:
                bits = " ".join(f"{k}={v}" for k, v in extra.items())
                lines.append(f"{'':<10} issues:  {bits}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
