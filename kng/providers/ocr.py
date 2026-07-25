"""OCR providers for newspaper cuttings + scanned PDFs.

Primary: Sarvam Document Intelligence (22 Indic scripts + English) — built for
scanned newspapers with mixed scripts, outputs clean Markdown. It's an async job
API: create job → upload → start → poll → download. The `sarvamai` SDK now wraps
this under `client().document_intelligence`, so we drive it through the SDK
instead of hand-rolling the REST schema.
Fallback: local Tesseract (needs `tel`/`hin` traineddata).
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import requests

# ISO-639-1 → BCP-47 the Document Intelligence job API expects (one per job).
_LANG_BCP47 = {
    "te": "te-IN", "hi": "hi-IN", "en": "en-IN", "ta": "ta-IN", "kn": "kn-IN",
    "ml": "ml-IN", "mr": "mr-IN", "bn": "bn-IN", "gu": "gu-IN", "pa": "pa-IN",
    "or": "or-IN", "ur": "ur-IN", "as": "as-IN",
}


def _bcp47(languages: list[str] | None) -> str:
    """The DI job takes a single language hint; use the first requested (this
    archive is Telugu-dominant), defaulting to Hindi per the API default."""
    if languages:
        return _LANG_BCP47.get(languages[0], "hi-IN")
    return "hi-IN"


# Document Intelligence rejects longer PDFs outright:
#   400 "PDF has 14 pages, maximum allowed is 10."
# Larger files are split into batches of this many pages, OCR'd as separate jobs
# and stitched back together with their original page numbers.
MAX_PDF_PAGES = 10


class SarvamOCR:
    def __init__(self, model: str = "sarvam-vision", max_pdf_pages: int = MAX_PDF_PAGES):
        self.model = model  # kept for config parity; DI job API selects the engine
        self.max_pdf_pages = max_pdf_pages
        self.last_call_jobs = 0   # billed DI jobs the most recent ocr_file() made

    def ocr_file(self, path: Path, languages: list[str] | None = None,
                 timeout_s: int = 600) -> list[tuple[int, str]]:
        """Return [(page_number, markdown_text), ...] for an image or PDF."""
        self.last_call_jobs = 0
        if path.suffix.lower() == ".pdf":
            import fitz
            with fitz.open(str(path)) as doc:
                n_pages = doc.page_count
            if n_pages > self.max_pdf_pages:
                return self._ocr_pdf_batched(path, languages, timeout_s, n_pages)
        return self._ocr_one(path, languages, timeout_s)

    def _ocr_one(self, path: Path, languages: list[str] | None,
                 timeout_s: int) -> list[tuple[int, str]]:
        from .sarvam import client
        di = client().document_intelligence
        job = di.create_job(language=_bcp47(languages), output_format="md")
        job.upload_file(str(path))
        job.start()
        self.last_call_jobs += 1
        job.wait_until_complete(poll_interval=3.0, timeout=timeout_s)
        return _collect_pages(di, job.job_id)

    def _ocr_pdf_batched(self, path: Path, languages: list[str] | None,
                         timeout_s: int, n_pages: int) -> list[tuple[int, str]]:
        """Split a long PDF into ≤max_pdf_pages slices, OCR each, renumber pages
        back to the original document so citations still resolve exactly."""
        import tempfile

        import fitz
        out: list[tuple[int, str]] = []
        with fitz.open(str(path)) as src, tempfile.TemporaryDirectory() as td:
            for start in range(0, n_pages, self.max_pdf_pages):
                end = min(start + self.max_pdf_pages, n_pages) - 1
                part_path = Path(td) / f"part_{start + 1:04d}.pdf"
                with fitz.open() as part:
                    part.insert_pdf(src, from_page=start, to_page=end)
                    part.save(str(part_path))
                for page_no, text in self._ocr_one(part_path, languages, timeout_s):
                    out.append((start + page_no, text))   # page_no is 1-based in-batch
        return out


class TesseractOCR:
    def __init__(self, langs: str = "tel+hin+eng"):
        self.langs = langs

    def ocr_file(self, path: Path, languages: list[str] | None = None) -> list[tuple[int, str]]:
        import pytesseract
        from PIL import Image
        if path.suffix.lower() == ".pdf":
            import fitz
            out = []
            doc = fitz.open(path)
            for i, page in enumerate(doc, start=1):
                pix = page.get_pixmap(dpi=300)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                out.append((i, pytesseract.image_to_string(img, lang=self.langs)))
            return out
        return [(1, pytesseract.image_to_string(Image.open(path), lang=self.langs))]


class NoOCR:
    def ocr_file(self, path: Path, languages: list[str] | None = None) -> list[tuple[int, str]]:
        return [(1, "")]


_TEXT_EXT = (".md", ".txt", ".html", ".htm")


def _pick_text_files(names: list[str]) -> list[str]:
    """Keep only the rendered text output. A DI job emits the Markdown/HTML render
    *and* a `.json` layout sidecar (coordinates/blocks) — the sidecar must not be
    ingested as content. Fall back to the raw set only if nothing else remains."""
    non_json = [n for n in names if not n.lower().endswith(".json")]
    text = [n for n in non_json if n.lower().endswith(_TEXT_EXT)]
    return text or non_json or names


def _collect_pages(di, job_id: str) -> list[tuple[int, str]]:
    """Download the DI job's text output and turn it into pages.

    DI may return one combined Markdown file (split on form-feed page markers) or
    one file per page (possibly zipped). We handle both, dropping JSON sidecars.
    """
    resp = di.get_download_links(job_id)
    urls = getattr(resp, "download_urls", None) or {}
    blobs: list[str] = []
    for name in _pick_text_files(sorted(urls)):
        url = getattr(urls[name], "file_url", None)
        if not url:
            continue
        content = requests.get(url, timeout=300).content
        if content[:2] == b"PK":  # zip of per-page files
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                for inner in _pick_text_files(sorted(z.namelist())):
                    blobs.append(z.read(inner).decode("utf-8", "replace"))
        else:
            blobs.append(content.decode("utf-8", "replace"))
    blobs = [strip_data_uris(b) for b in blobs]
    if not blobs:
        return [(1, "")]
    if len(blobs) == 1:                       # single combined doc
        return split_pages(blobs[0])
    return [(i, b) for i, b in enumerate(blobs, start=1)]


# DI embeds every figure it finds as a base64 data URI in the Markdown. Left in,
# these dwarf the real text (they were 89% of the first full pass) and would be
# chunked and embedded as if they were content.
_DATA_URI = re.compile(r"!\[[^\]]*\]\(\s*data:[^)]*\)")
# DI separates pages with a horizontal rule, not a form feed.
_PAGE_BREAK = re.compile(r"(?m)^---+[ \t]*$")


def strip_data_uris(text: str) -> str:
    """Drop base64 image payloads from DI Markdown, keeping the surrounding text."""
    return _DATA_URI.sub("", text)


def split_pages(markdown: str) -> list[tuple[int, str]]:
    """Split combined markdown into pages.

    DI marks page boundaries with a `---` rule (form feeds appear in other
    engines' output, so both are handled). Without this every multi-page PDF
    collapses into a single page-1 segment and citations cannot resolve.
    """
    if "\f" in markdown:
        parts = markdown.split("\f")
    else:
        parts = _PAGE_BREAK.split(markdown)
    return [(i + 1, p) for i, p in enumerate(parts)] or [(1, markdown)]
