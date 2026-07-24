"""Ingestion orchestrator.

    python -m kng.pipeline.run --stage all
    python -m kng.pipeline.run --stage extract --only "10_28.11.2024*" --no-asr
    python -m kng.pipeline.run --dry-run

Incremental: a content-hash manifest skips files already done for a stage.
Extract writes one ExtractedDoc JSON per source file under `extracted/`.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

from ..config import ROOT, settings
from ..models import ExtractedDoc
from . import metadata
from .extract import extract_file
from .manifest import Manifest
from .normalize import normalize_doc

SKIP_PREFIXES = ("~$", ".~", "._")
EXTRACTED = ROOT / "extracted"
MANIFEST_PATH = ROOT / "index" / "manifest.json"


def discover(data_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(data_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith(SKIP_PREFIXES):
            continue
        if metadata.source_type_for(p) is None:
            continue
        files.append(p)
    return files


def _extracted_path(rel: str) -> Path:
    return EXTRACTED / (rel + ".json")


def load_extracted(rel: str) -> ExtractedDoc | None:
    fp = _extracted_path(rel)
    if fp.exists():
        return ExtractedDoc.model_validate_json(fp.read_text(encoding="utf-8"))
    return None


def _save_extracted(doc: ExtractedDoc) -> None:
    fp = _extracted_path(doc.source_file)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(doc.model_dump_json(indent=2), encoding="utf-8")


def run_extract(files: list[Path], man: Manifest, *, use_ocr: bool, use_asr: bool,
                translate: bool, force: bool) -> dict:
    stats = {"processed": 0, "skipped": 0, "segments": 0, "errors": 0}
    for i, path in enumerate(files, start=1):
        rel = str(path.relative_to(ROOT))
        if not force and not man.needs(rel, path, "extract"):
            stats["skipped"] += 1
            continue
        doc = extract_file(path, settings().data_dir, use_ocr=use_ocr, use_asr=use_asr)
        normalize_doc(doc, translate=translate)
        _save_extracted(doc)
        man.mark(rel, "extract", "error" if doc.error else "done")
        if not doc.error:
            man.mark(rel, "normalize", "done")
        stats["processed"] += 1
        stats["segments"] += len(doc.segments)
        stats["errors"] += 1 if doc.error else 0
        flag = "ERR" if doc.error else f"{len(doc.segments)}seg"
        print(f"[{i}/{len(files)}] {flag:>7}  {rel}", file=sys.stderr)
        if i % 25 == 0:
            man.save()
    man.save()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.pipeline.run")
    ap.add_argument("--stage", default="all",
                    choices=["all", "extract", "chunk", "embed", "graph"])
    ap.add_argument("--only", help="glob on relative path to limit files/meets")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-ocr", action="store_true", help="skip image/scanned-PDF OCR")
    ap.add_argument("--no-asr", action="store_true", help="skip video ASR")
    ap.add_argument("--translate", action="store_true", help="also store English translation")
    ap.add_argument("--force", action="store_true", help="ignore manifest, reprocess")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    s = settings()
    files = discover(s.data_dir)
    if args.only:
        files = [f for f in files if fnmatch.fnmatch(str(f.relative_to(ROOT)), f"*{args.only}*")]
    if args.limit:
        files = files[: args.limit]

    print(f"discovered {len(files)} source files under {s.data_dir}", file=sys.stderr)
    if args.dry_run:
        by_type: dict[str, int] = {}
        for f in files:
            t = metadata.source_type_for(f)
            by_type[t.value if t else "?"] = by_type.get(t.value if t else "?", 0) + 1
        print(json.dumps(by_type, indent=2))
        return 0

    man = Manifest(MANIFEST_PATH)

    if args.stage in ("all", "extract"):
        stats = run_extract(files, man, use_ocr=not args.no_ocr, use_asr=not args.no_asr,
                            translate=args.translate, force=args.force)
        print("extract:", json.dumps(stats), file=sys.stderr)

    if args.stage in ("all", "chunk"):
        _run_later_stage("chunk", man, files)
    if args.stage in ("all", "embed"):
        _run_later_stage("embed", man, files)
    if args.stage in ("all", "graph"):
        _run_later_stage("graph", man, files)

    print("manifest:", json.dumps(man.summary()), file=sys.stderr)
    return 0


def _run_later_stage(stage: str, man: Manifest, files: list[Path]) -> None:
    """chunk/embed/graph land in WP2/WP3. Dispatch here when implemented."""
    try:
        if stage == "chunk":
            from .chunk import run_chunk
            run_chunk(man, files)
        elif stage == "embed":
            from .embed import run_embed
            run_embed(man, files)
        elif stage == "graph":
            from .graph_build import run_graph
            run_graph(man, files)
    except ImportError:
        print(f"[{stage}] not implemented yet — skipping", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
