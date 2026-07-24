"""OCR providers for newspaper cuttings + scanned PDFs.

Primary: Sarvam Document Intelligence (sarvam-vision, 22 Indic + English) — built
for scanned newspapers with mixed scripts, outputs clean Markdown. Async job API.
Fallback: local Tesseract (needs `tel`/`hin` traineddata).

NOTE: the exact Sarvam doc-digitization request schema is validated live in WP1;
the flow below is defensive (handles both sync-text and async-job responses).
"""
from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import requests

from .sarvam import SARVAM_BASE, _headers

_OCR_JOB = f"{SARVAM_BASE}/doc-digitization/job/v1"


class SarvamOCR:
    def __init__(self, model: str = "sarvam-vision"):
        self.model = model

    def ocr_file(self, path: Path, languages: list[str] | None = None) -> list[tuple[int, str]]:
        """Return [(page_number, markdown_text), ...] for an image or PDF."""
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, _mime(path))}
            data = {"model": self.model}
            if languages:
                data["language"] = ",".join(languages)
            resp = requests.post(_OCR_JOB, headers=_headers(), files=files, data=data, timeout=120)
        resp.raise_for_status()
        payload = resp.json()

        # (a) synchronous text response
        for key in ("markdown", "output", "text", "content"):
            if isinstance(payload, dict) and payload.get(key):
                return _paginate(str(payload[key]))

        # (b) async job → poll → download
        job_id = payload.get("job_id") or payload.get("id")
        if not job_id:
            return [(1, "")]
        return self._await_job(job_id)

    def _await_job(self, job_id: str, timeout_s: int = 300) -> list[tuple[int, str]]:
        deadline = time.time() + timeout_s
        status_url = f"{_OCR_JOB}/{job_id}"
        while time.time() < deadline:
            r = requests.get(status_url, headers=_headers(), timeout=60)
            r.raise_for_status()
            js = r.json()
            state = (js.get("status") or js.get("state") or "").lower()
            if state in {"done", "completed", "succeeded", "success"}:
                out_url = js.get("output_url") or js.get("output") or js.get("result_url")
                if out_url:
                    return _download_output(out_url)
                for key in ("markdown", "text", "content"):
                    if js.get(key):
                        return _paginate(str(js[key]))
                return [(1, "")]
            if state in {"failed", "error"}:
                raise RuntimeError(f"Sarvam OCR job {job_id} failed: {js}")
            time.sleep(3)
        raise TimeoutError(f"Sarvam OCR job {job_id} timed out")


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


def _mime(path: Path) -> str:
    return {
        ".pdf": "application/pdf", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(path.suffix.lower(), "application/octet-stream")


def _paginate(markdown: str) -> list[tuple[int, str]]:
    """Split combined markdown into pages on form-feed / page markers, else one page."""
    if "\f" in markdown:
        return [(i + 1, p) for i, p in enumerate(markdown.split("\f"))]
    return [(1, markdown)]


def _download_output(url: str) -> list[tuple[int, str]]:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    content = r.content
    if content[:2] == b"PK":  # zip
        pages = []
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for i, name in enumerate(sorted(z.namelist()), start=1):
                if name.endswith((".md", ".txt", ".json")):
                    pages.append((i, z.read(name).decode("utf-8", "replace")))
        return pages or [(1, "")]
    return _paginate(content.decode("utf-8", "replace"))
