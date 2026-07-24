"""Extraction dispatch: one source file -> ExtractedDoc (metadata + segments).

Text formats (docx/pdf/pptx/xlsx) run with no external calls. Images route to
the OCR provider and videos to the ASR provider — gated by `use_ocr`/`use_asr`
so a dry run never triggers paid calls.
"""
from __future__ import annotations

from pathlib import Path

from ...config import ROOT, settings
from ...models import ExtractedDoc, SourceType
from .. import metadata
from . import documents, media

_TEXT_DISPATCH = {
    SourceType.press_release: documents.extract_doc,
    SourceType.source_doc: documents.extract_pdf,
    SourceType.slide: documents.extract_pptx,
    SourceType.table: documents.extract_xlsx,
}


def extract_file(abs_path: Path, data_root: Path | None = None,
                 use_ocr: bool = True, use_asr: bool = True) -> ExtractedDoc:
    data_root = data_root or settings().data_dir
    meta = metadata.derive(abs_path, data_root)
    rel = str(abs_path.relative_to(ROOT))
    stype = meta["source_type"]

    doc = ExtractedDoc(
        source_file=rel,
        source_type=stype or SourceType.source_doc,
        press_meet_id=meta["press_meet_id"],
        press_meet_title=meta["press_meet_title"],
        date=meta["date"],
        topic=meta["topic"],
        publication=meta["publication"],
    )
    try:
        if stype == SourceType.news_clip:
            doc.segments = media.extract_image(abs_path, meta, rel, use_ocr=use_ocr)
            doc.extractor = "ocr"
        elif stype == SourceType.video:
            doc.segments = media.extract_video(abs_path, meta, rel, use_asr=use_asr)
            doc.extractor = "asr"
        elif stype in _TEXT_DISPATCH:
            fn = _TEXT_DISPATCH[stype]
            doc.segments = fn(abs_path, meta, rel, use_ocr=use_ocr)
            doc.extractor = fn.__name__
        else:
            doc.error = f"no extractor for {abs_path.suffix}"
    except Exception as e:  # never let one bad file kill the batch
        doc.error = f"{type(e).__name__}: {e}"
    return doc
