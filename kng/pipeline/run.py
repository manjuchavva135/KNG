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
# Directories written by Office / Bromium micro-VM isolation. They hold
# byte-sized stubs that mirror real filenames, so they look like source files
# but are not decodable (Sarvam OCR rejects them as "corrupted image file").
SKIP_DIR_PREFIXES = ("~",)
EXTRACTED = ROOT / "extracted"
MANIFEST_PATH = ROOT / "index" / "manifest.json"


def discover(data_dir: Path) -> list[Path]:
    files: list[Path] = []
    for p in sorted(data_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.name.startswith(SKIP_PREFIXES):
            continue
        if any(d.startswith(SKIP_DIR_PREFIXES) for d in p.relative_to(data_dir).parts[:-1]):
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
                sarvam: bool, use_cleanup: bool, translate: bool, force: bool) -> dict:
    counts: dict = {"total": len(files), "processed": 0, "skipped": 0,
                    "errors": 0, "segments": 0, "by_type": {}, "sarvam_calls": {}}
    for i, path in enumerate(files, start=1):
        rel = str(path.relative_to(ROOT))
        # Always call needs(): it is what registers the file in the manifest.
        # Short-circuiting it under --force left the manifest empty, so the
        # final save() wiped the previous run's tracking.
        stale = man.needs(rel, path, "extract")
        if not force and not stale:
            counts["skipped"] += 1
            continue
        doc = extract_file(path, settings().data_dir, use_ocr=use_ocr, use_asr=use_asr,
                           sarvam=sarvam, use_cleanup=use_cleanup)
        normalize_doc(doc, translate=translate)
        entry = man.entries.get(rel)
        if entry is not None:                       # makes extracted/ self-describing
            doc.file_hash = entry.sha1
        _save_extracted(doc)
        man.mark(rel, "extract", "error" if doc.error else "done")
        if not doc.error:
            man.mark(rel, "normalize", "done")
        counts["processed"] += 1
        counts["segments"] += len(doc.segments)
        counts["errors"] += 1 if doc.error else 0
        t = doc.source_type.value
        counts["by_type"][t] = counts["by_type"].get(t, 0) + 1
        for k, v in (doc.sarvam_calls or {}).items():
            counts["sarvam_calls"][k] = counts["sarvam_calls"].get(k, 0) + v
        flag = "ERR" if doc.error else f"{len(doc.segments)}seg"
        print(f"[{i}/{len(files)}] {flag:>7}  {rel}", file=sys.stderr)
        if i % 25 == 0:
            man.save()
    man.save()
    return counts


def rebuild_manifest(files: list[Path], man: Manifest) -> dict:
    """Reconstruct manifest state from the `extracted/` docs already on disk.

    Recovery path for a lost/clobbered manifest: it re-derives per-file hashes
    and stage status from completed work instead of re-running extraction, so
    no paid Sarvam calls are repeated.
    """
    counts = {"total": len(files), "restored": 0, "errors": 0, "missing": 0}
    for path in files:
        rel = str(path.relative_to(ROOT))
        doc = load_extracted(rel)
        if doc is None:
            counts["missing"] += 1
            continue
        man.needs(rel, path, "extract")             # registers/refreshes the entry
        man.mark(rel, "extract", "error" if doc.error else "done")
        if not doc.error:
            man.mark(rel, "normalize", "done")
            counts["restored"] += 1
        else:
            counts["errors"] += 1
    man.save()
    return counts


def repair_extracted(files: list[Path], man: Manifest) -> dict:
    """Re-clean `extracted/` docs in place — no extraction, no paid calls.

    The first paid pass stored Sarvam DI Markdown verbatim, which meant two
    defects baked into the artifacts:
      * base64 image data URIs kept as if they were text (89% of the corpus);
      * multi-page PDFs collapsed into one page-1 segment, because DI separates
        pages with a `---` rule rather than a form feed.
    Both are recoverable from what is already on disk, so this repairs the
    artifacts instead of re-paying for OCR. Idempotent.
    """
    from ..models import Locator
    from ..providers.ocr import split_pages, strip_data_uris
    from .extract.documents import _MIN_TEXT

    counts = {"docs": 0, "changed": 0, "chars_removed": 0,
              "segments_before": 0, "segments_after": 0, "repaginated": 0}
    for path in files:
        rel = str(path.relative_to(ROOT))
        doc = load_extracted(rel)
        if doc is None or not doc.segments:
            continue
        counts["docs"] += 1
        before_chars = sum(len(s.text_original) for s in doc.segments)
        counts["segments_before"] += len(doc.segments)

        for seg in doc.segments:
            seg.text_original = strip_data_uris(seg.text_original).strip()

        # An OCR'd PDF that came back as a single blob still holds its page rules.
        if ((doc.sarvam_calls or {}).get("ocr") and path.suffix.lower() == ".pdf"
                and len(doc.segments) == 1):
            base = doc.segments[0]
            pages = split_pages(base.text_original)
            if len(pages) > 1:
                rebuilt = []
                for page_no, text in pages:
                    text = text.strip()
                    if len(text) < _MIN_TEXT:
                        continue
                    seg = base.model_copy(deep=True)
                    seg.text_original = text
                    seg.locator = Locator(page=page_no)
                    seg.segment_id = f"{rel}#p{page_no}"
                    rebuilt.append(seg)
                if rebuilt:
                    doc.segments = rebuilt
                    counts["repaginated"] += 1

        doc.segments = [s for s in doc.segments if len(s.text_original) >= _MIN_TEXT]
        normalize_doc(doc, translate=False)     # re-detect language on clean text
        after_chars = sum(len(s.text_original) for s in doc.segments)
        counts["segments_after"] += len(doc.segments)
        if after_chars != before_chars:
            counts["changed"] += 1
            counts["chars_removed"] += before_chars - after_chars
            _save_extracted(doc)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kng.pipeline.run")
    ap.add_argument("--stage", default="all",
                    choices=["all", "extract", "chunk", "embed", "graph"])
    ap.add_argument("--only", help="glob on relative path to limit files/meets")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-ocr", action="store_true", help="skip image/PDF OCR")
    ap.add_argument("--no-asr", action="store_true", help="skip video ASR")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="skip Sarvam LLM cleanup of office docs (use raw local parse)")
    ap.add_argument("--local-only", action="store_true",
                    help="fully offline: no paid Sarvam calls (local parse only)")
    ap.add_argument("--translate", action="store_true", help="also store English translation")
    ap.add_argument("--force", action="store_true", help="ignore manifest, reprocess")
    ap.add_argument("--rebuild-manifest", action="store_true",
                    help="rebuild index/manifest.json from existing extracted/ docs "
                         "(no extraction, no paid calls) and exit")
    ap.add_argument("--repair-extracted", action="store_true",
                    help="strip base64 image payloads + re-paginate OCR'd docs "
                         "already in extracted/ (no paid calls) and exit")
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

    if args.repair_extracted:
        c = repair_extracted(files, man)
        print(f"repaired {c['changed']}/{c['docs']} docs · "
              f"{c['chars_removed']:,} chars of base64 removed · "
              f"{c['repaginated']} PDFs re-paginated · "
              f"segments {c['segments_before']} → {c['segments_after']}", file=sys.stderr)
        return 0

    if args.rebuild_manifest:
        counts = rebuild_manifest(files, man)
        print(f"rebuilt manifest from extracted/: {counts['restored']} done · "
              f"{counts['errors']} error · {counts['missing']} not extracted",
              file=sys.stderr)
        print("manifest:", json.dumps(man.summary()), file=sys.stderr)
        return 0

    if args.stage in ("all", "extract"):
        sarvam = not args.local_only
        translate = args.translate and sarvam        # translation is a Sarvam call
        counts = run_extract(
            files, man, use_ocr=not args.no_ocr, use_asr=not args.no_asr,
            sarvam=sarvam, use_cleanup=not args.no_cleanup,
            translate=translate, force=args.force,
        )
        from ..stats import set_stage
        set_stage("extract", counts)
        n_calls = sum(counts["sarvam_calls"].values())
        mode = "local-only" if args.local_only else "sarvam"
        print(f"extract [{mode}]: {counts['processed']}/{counts['total']} done · "
              f"{counts['segments']} seg · {counts['errors']} err · "
              f"{n_calls} sarvam calls", file=sys.stderr)

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
