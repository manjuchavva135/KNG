"""Text-format extractors: docx/doc, pdf, pptx, xlsx. No external calls except
optional OCR of scanned PDF pages (gated by use_ocr).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ...models import Locator, Segment, SourceType

# A page/segment with fewer than this many non-space chars is treated as scanned.
_MIN_TEXT = 12


def _seg(meta: dict, rel: str, sid: str, text: str, locator: Locator,
         stype: SourceType | None = None) -> Segment:
    return Segment(
        segment_id=sid,
        source_file=rel,
        source_type=stype or meta["source_type"],
        press_meet_id=meta["press_meet_id"],
        press_meet_title=meta["press_meet_title"],
        date=meta["date"],
        topic=meta["topic"],
        publication=meta["publication"],
        locator=locator,
        text_original=text.strip(),
    )


# ── DOCX / DOC ─────────────────────────────────────────────────────────────────
def extract_doc(path: Path, meta: dict, rel: str, use_ocr: bool = True) -> list[Segment]:
    if path.suffix.lower() == ".doc":
        return _extract_legacy_doc(path, meta, rel)
    import docx
    d = docx.Document(str(path))
    parts: list[str] = [p.text for p in d.paragraphs if p.text and p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    text = "\n".join(parts)
    if not text.strip():
        return []
    return [_seg(meta, rel, f"{rel}#doc", text, Locator())]


def _extract_legacy_doc(path: Path, meta: dict, rel: str) -> list[Segment]:
    """Best-effort .doc via LibreOffice headless -> txt. Skips if unavailable."""
    import shutil
    import tempfile
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("legacy .doc needs LibreOffice (soffice) on PATH")
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([soffice, "--headless", "--convert-to", "txt:Text",
                        "--outdir", td, str(path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out = Path(td) / (path.stem + ".txt")
        text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
    return [_seg(meta, rel, f"{rel}#doc", text, Locator())] if text.strip() else []


# ── PDF ────────────────────────────────────────────────────────────────────────
def extract_pdf(path: Path, meta: dict, rel: str, use_ocr: bool = True) -> list[Segment]:
    import fitz
    doc = fitz.open(str(path))
    segs: list[Segment] = []
    scanned_pages: list[int] = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        if len(text.strip()) >= _MIN_TEXT:
            segs.append(_seg(meta, rel, f"{rel}#p{i}", text, Locator(page=i)))
        else:
            scanned_pages.append(i)
    # OCR the scanned pages if enabled
    if scanned_pages and use_ocr:
        from ...providers import get_ocr
        ocr = get_ocr()
        for page_no, text in _ocr_pdf_pages(ocr, path):
            if len(text.strip()) >= _MIN_TEXT and page_no in scanned_pages:
                segs.append(_seg(meta, rel, f"{rel}#p{page_no}", text,
                                 Locator(page=page_no), SourceType.source_doc))
    segs.sort(key=lambda s: s.locator.page or 0)
    return segs


def _ocr_pdf_pages(ocr, path: Path):
    try:
        return ocr.ocr_file(path, languages=["te", "hi", "en"])
    except Exception:
        return []


# ── PPTX ───────────────────────────────────────────────────────────────────────
def extract_pptx(path: Path, meta: dict, rel: str, use_ocr: bool = True) -> list[Segment]:
    from pptx import Presentation
    prs = Presentation(str(path))
    segs: list[Segment] = []
    for i, slide in enumerate(prs.slides, start=1):
        lines: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            note = slide.notes_slide.notes_text_frame.text.strip()
            if note:
                lines.append(f"[notes] {note}")
        text = "\n".join(lines)
        if text.strip():
            segs.append(_seg(meta, rel, f"{rel}#s{i}", text, Locator(slide=i)))
    return segs


# ── XLSX ───────────────────────────────────────────────────────────────────────
def extract_xlsx(path: Path, meta: dict, rel: str, use_ocr: bool = True) -> list[Segment]:
    import pandas as pd
    sheets = pd.read_excel(str(path), sheet_name=None, header=None, dtype=str)
    segs: list[Segment] = []
    for idx, (name, df) in enumerate(sheets.items(), start=1):
        df = df.fillna("")
        rows = ["\t".join(str(c) for c in row) for row in df.values.tolist()[:200]]
        text = f"# Sheet: {name}\n" + "\n".join(r for r in rows if r.strip())
        if text.strip():
            segs.append(_seg(meta, rel, f"{rel}#sheet{idx}", text, Locator()))
    return segs
