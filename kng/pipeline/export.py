"""WP6 portable export — one archive that makes another machine a working system.

    python -m kng.pipeline.export --plan                 # what would go in, and how big
    python -m kng.pipeline.export --out kng-index.tar.gz # build it + a .sha256
    python -m kng.pipeline.export --verify kng-index.tar.gz

`scripts/package_index.sh` did the tarring already; this adds the parts a shell
one-liner cannot: an in-archive `EXPORT.json` recording *what* was packaged
(counts, embedding model, graph size, git commit) and a `--verify` that re-reads
the archive and checks it against that record.

Why the record matters: the vectors in `index/lancedb` are meaningless to a
machine using a different embedding model, and there is nothing in a tarball to
say which model produced them. A silent mismatch does not crash — it returns
plausible, wrong neighbours. `EXPORT.json` names the model (and the dimension), so
`--verify` can refuse instead.

**Never exported:** `.env` (the API key), `var/` (password hashes, real user
questions), and `/data/` (584 MB of source media that querying does not need).
The exclusion is by allow-list, not by pattern — a deny-list quietly grows holes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config import settings

RECORD_NAME = "EXPORT.json"

# Allow-list of what a query machine needs, in the order it is packed.
# `extracted/` is what citations resolve against; without it the app still answers
# but the source viewer has nothing to show.
PARTS: tuple[tuple[str, bool], ...] = (
    ("index/manifest.json", True),
    ("index/stats.json", False),
    ("index/chunks", True),
    ("index/lancedb", True),
    ("index/graph", False),          # absent before WP3 has run
    ("extracted", False),            # optional via --no-extracted
    ("config/ontology.yaml", False),
)


# ── inventory ──────────────────────────────────────────────────────────────────
@dataclass
class Part:
    path: Path
    rel: str
    required: bool
    files: int = 0
    bytes: int = 0

    @property
    def exists(self) -> bool:
        return self.path.exists()


def _walk(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    for p in sorted(path.rglob("*")):
        if p.is_file():
            yield p


def inventory(root: Optional[Path] = None, *, with_extracted: bool = True) -> list[Part]:
    root = root or settings().path(".")
    parts: list[Part] = []
    for rel, required in PARTS:
        if rel == "extracted" and not with_extracted:
            continue
        path = root / rel
        part = Part(path=path, rel=rel, required=required)
        if part.exists:
            for f in _walk(path):
                part.files += 1
                part.bytes += f.stat().st_size
        parts.append(part)
    return parts


def missing_required(parts: Iterable[Part]) -> list[str]:
    return [p.rel for p in parts if p.required and not p.exists]


# ── the record that travels with the archive ───────────────────────────────────
def _git_commit(root: Path) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _counts(root: Path) -> dict[str, Any]:
    """Facts a receiving machine can check its own state against."""
    counts: dict[str, Any] = {}
    stats = root / "index/stats.json"
    if stats.exists():
        try:
            blob = json.loads(stats.read_text(encoding="utf-8"))
            counts["stats"] = blob if isinstance(blob, dict) else {}
        except ValueError:
            pass
    graph = root / "index/graph/graph.json"
    if graph.exists():
        try:
            g = json.loads(graph.read_text(encoding="utf-8"))
            counts["graph"] = {"nodes": len(g.get("nodes", [])),
                               "edges": len(g.get("edges", []))}
        except ValueError:
            pass
    chunks_dir = root / "index/chunks"
    if chunks_dir.exists():
        counts["chunk_files"] = sum(1 for _ in chunks_dir.rglob("*.json"))

    # Read the vector dimension from the table itself rather than from config: the
    # archive should record what the vectors *are*, not what a `.env` claims.
    # Only when exporting the configured project root — `settings().lancedb_path`
    # resolves against that root, so for any other root it would describe the wrong
    # store, which is worse than describing none.
    if root.resolve() == settings().path(".").resolve():
        try:
            from ..store import vector
            table = vector.open_table()
            counts["vectors"] = {"rows": table.count_rows(),
                                 "dim": vector.table_dim(table)}
        except Exception:                 # a missing/older store must not block export
            pass
    return counts


def build_record(parts: list[Part], root: Path, *, note: str = "") -> dict[str, Any]:
    cfg = settings()
    counts = _counts(root)
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": "kng.pipeline.export",
        "format": 1,
        "git_commit": _git_commit(root),
        # The vectors are only meaningful under the model that produced them.
        "embed_model": cfg.local_embed_model,
        "embed_dim": (counts.get("vectors") or {}).get("dim"),
        "parts": [{"path": p.rel, "files": p.files, "bytes": p.bytes,
                   "present": p.exists} for p in parts],
        "totals": {"files": sum(p.files for p in parts),
                   "bytes": sum(p.bytes for p in parts)},
        "counts": counts,
        "note": note,
        "excluded": [".env (API key)", "var/ (password hashes, user questions)",
                     "data/ (source media, not needed to query)"],
    }


# ── building ───────────────────────────────────────────────────────────────────
def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _safe_name(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


def build(out: Path, *, root: Optional[Path] = None, with_extracted: bool = True,
          note: str = "", progress=None) -> dict[str, Any]:
    """Write the archive and its `.sha256`. Returns the record that went inside."""
    root = root or settings().path(".")
    parts = inventory(root, with_extracted=with_extracted)
    absent = missing_required(parts)
    if absent:
        raise FileNotFoundError(
            "cannot export without " + ", ".join(absent)
            + " — run the pipeline first (`python -m kng.pipeline.run --stage all`)")

    record = build_record(parts, root, note=note)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tarfile.open(out, "w:gz") as tar:
        for part in parts:
            if not part.exists:
                continue
            for f in _walk(part.path):
                tar.add(f, arcname=_safe_name(root, f))
            if progress:
                progress(part)
        # The record goes in last, so it describes what is already inside.
        blob = json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")
        info = tarfile.TarInfo(RECORD_NAME)
        info.size = len(blob)
        info.mtime = int(time.time())
        tar.addfile(info, __import__("io").BytesIO(blob))

    digest = sha256(out)
    (out.parent / f"{out.name}.sha256").write_text(f"{digest}  {out.name}\n",
                                                   encoding="utf-8")
    record["archive"] = {"name": out.name, "bytes": out.stat().st_size,
                         "sha256": digest}
    return record


# ── verifying ──────────────────────────────────────────────────────────────────
def read_record(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:*") as tar:
        try:
            fh = tar.extractfile(RECORD_NAME)
        except KeyError:
            fh = None
        if fh is None:
            raise ValueError(f"{archive.name} has no {RECORD_NAME} — it was not "
                             f"written by kng.pipeline.export")
        return json.loads(fh.read().decode("utf-8"))


def verify(archive: Path) -> dict[str, Any]:
    """Check the archive against its own record, plus the sidecar checksum.

    Reports rather than raises for content mismatches: an operator who has just
    copied 200 MB over a slow link needs to know *what* is wrong.
    """
    problems: list[str] = []
    record = read_record(archive)

    sidecar = archive.parent / f"{archive.name}.sha256"
    digest = sha256(archive)
    if sidecar.exists():
        expected = sidecar.read_text(encoding="utf-8").split()[0]
        if expected != digest:
            problems.append(f"checksum mismatch: {sidecar.name} says {expected[:12]}…, "
                            f"archive is {digest[:12]}…")
    else:
        problems.append(f"no {sidecar.name} beside the archive — integrity unverified")

    with tarfile.open(archive, "r:*") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
    names = {m.name for m in members}
    # Path traversal in a tarball is a real attack; refuse before anyone extracts.
    for m in members:
        if m.name.startswith("/") or ".." in Path(m.name).parts:
            problems.append(f"unsafe member path: {m.name}")

    counted = len(members) - (1 if RECORD_NAME in names else 0)
    claimed = (record.get("totals") or {}).get("files")
    if claimed is not None and claimed != counted:
        problems.append(f"file count mismatch: record says {claimed}, archive has {counted}")

    for part in record.get("parts", []):
        if not part.get("present"):
            continue
        prefix = part["path"]
        if not any(n == prefix or n.startswith(prefix.rstrip("/") + "/") for n in names):
            problems.append(f"record lists {prefix} but the archive has no such member")

    current = settings().local_embed_model
    if record.get("embed_model") and record["embed_model"] != current:
        problems.append(
            f"embedding model differs: archive was built with "
            f"{record['embed_model']}, this machine is configured for {current}. "
            f"The vectors are only meaningful under the model that produced them "
            f"— set LOCAL_EMBED_MODEL to match before querying.")

    return {"archive": archive.name, "sha256": digest, "files": counted,
            "record": record, "problems": problems, "ok": not problems}


def extract(archive: Path, dest: Path) -> int:
    """Extract after checking every member path. Returns the number of files."""
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    count = 0
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            target = (resolved_dest / member.name).resolve()
            if not (target == resolved_dest or resolved_dest in target.parents):
                raise ValueError(f"refusing to extract outside {dest}: {member.name}")
        for member in tar.getmembers():
            tar.extract(member, path=dest)
            if member.isfile():
                count += 1
    return count


# ── CLI ────────────────────────────────────────────────────────────────────────
def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.pipeline.export",
                                 description="Package index/ + extracted/ for another machine")
    ap.add_argument("--out", type=Path, help="archive to write (default kng-index-<date>.tar.gz)")
    ap.add_argument("--plan", action="store_true", help="list what would be packaged, write nothing")
    ap.add_argument("--verify", type=Path, metavar="ARCHIVE", help="check an existing archive")
    ap.add_argument("--extract", type=Path, metavar="ARCHIVE", help="extract an archive")
    ap.add_argument("--dest", type=Path, help="destination for --extract")
    ap.add_argument("--no-extracted", action="store_true",
                    help="omit extracted/ (smaller, but citations cannot open their passage)")
    ap.add_argument("--note", default="", help="free-text note stored in EXPORT.json")
    args = ap.parse_args(argv)

    if args.verify:
        out = verify(args.verify)
        rec = out["record"]
        print(f"{out['archive']}  {out['files']} files  sha256 {out['sha256'][:16]}…")
        print(f"  built    : {rec.get('created_at')} · commit {(rec.get('git_commit') or '?')[:8]}")
        print(f"  embedding: {rec.get('embed_model')}")
        counts = rec.get("counts") or {}
        if counts.get("graph"):
            print(f"  graph    : {counts['graph']['nodes']} nodes / "
                  f"{counts['graph']['edges']} edges")
        if counts.get("chunk_files"):
            print(f"  chunks   : {counts['chunk_files']} files")
        for part in rec.get("parts", []):
            if part.get("present"):
                print(f"  {part['path']:<22} {part['files']:>6} files  "
                      f"{_human(part['bytes']):>9}")
        if out["ok"]:
            print("\nOK — archive matches its record")
            return 0
        print("\nproblems:", file=sys.stderr)
        for p in out["problems"]:
            print(f"  - {p}", file=sys.stderr)
        return 1

    if args.extract:
        dest = args.dest or Path.cwd()
        n = extract(args.extract, dest)
        print(f"extracted {n} files into {dest}")
        return 0

    parts = inventory(with_extracted=not args.no_extracted)
    absent = missing_required(parts)
    print("would package:" if args.plan else "packaging:")
    for p in parts:
        mark = "  " if p.exists else "??"
        state = f"{p.files:>6} files  {_human(p.bytes):>9}" if p.exists else "missing"
        print(f" {mark} {p.rel:<22} {state}"
              + ("  (required)" if p.required and not p.exists else ""))
    total = sum(p.bytes for p in parts)
    print(f"    {'total':<24} {sum(p.files for p in parts):>6} files  {_human(total):>9}"
          f"  (compresses to roughly half)")
    if args.no_extracted:
        print("\n  ⚠ extracted/ omitted — the app will answer, but a citation "
              "cannot open its passage.")
    if absent:
        print(f"\nerror: missing required {', '.join(absent)}", file=sys.stderr)
        return 1
    if args.plan:
        return 0

    out = args.out or Path(f"kng-index-{time.strftime('%Y%m%d-%H%M')}.tar.gz")
    print()
    record = build(out, with_extracted=not args.no_extracted, note=args.note,
                   progress=lambda p: print(f"  packed {p.rel}", flush=True))
    arch = record["archive"]
    print(f"\n  archive : {out}  ({_human(arch['bytes'])})")
    print(f"  checksum: {out.name}.sha256  ({arch['sha256'][:16]}…)")
    print(f"\nOn the target machine:\n"
          f"  sha256sum -c {out.name}.sha256\n"
          f"  python -m kng.pipeline.export --verify {out.name}\n"
          f"  tar -xzf {out.name} && pip install -e '.[local]'\n"
          f"  python -m kng.graph_query stats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
