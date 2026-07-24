"""Content-hash manifest → idempotent, incremental ingestion.

Each source file is tracked by (path, sha1, size, mtime) and a per-stage status.
Re-running the pipeline only reprocesses files whose content changed or whose
stage hasn't completed. This is what makes the system scalable as new press
meets are dropped into `data/`.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

STAGES = ["extract", "normalize", "chunk", "embed", "graph"]


def file_hash(path: Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while block := f.read(chunk_size):
            h.update(block)
    return h.hexdigest()


@dataclass
class Entry:
    path: str
    sha1: str
    size: int
    mtime: float
    stages: dict[str, str] = field(default_factory=dict)   # stage -> "done"|"error"|status
    updated: float = field(default_factory=time.time)


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, Entry] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                self.entries[k] = Entry(**v)

    def _key(self, rel_path: str) -> str:
        return rel_path

    def needs(self, rel_path: str, abs_path: Path, stage: str) -> bool:
        """True if `stage` must run for this file (new / changed / not done)."""
        sha = file_hash(abs_path)
        e = self.entries.get(rel_path)
        if e is None or e.sha1 != sha:
            self.entries[rel_path] = Entry(
                path=rel_path, sha1=sha, size=abs_path.stat().st_size,
                mtime=abs_path.stat().st_mtime,
            )
            return True
        return e.stages.get(stage) != "done"

    def mark(self, rel_path: str, stage: str, status: str = "done") -> None:
        e = self.entries.get(rel_path)
        if e is None:
            return
        e.stages[stage] = status
        e.updated = time.time()

    def status(self, rel_path: str, stage: str) -> Optional[str]:
        e = self.entries.get(rel_path)
        return e.stages.get(stage) if e else None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: asdict(v) for k, v in self.entries.items()}
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def summary(self) -> dict[str, int]:
        out = {"files": len(self.entries)}
        for s in STAGES:
            out[s] = sum(1 for e in self.entries.values() if e.stages.get(s) == "done")
        return out
