# Handover — WP1: Multimodal Extraction + Normalization

**Status:** ✅ Text extraction done & verified on the full corpus. OCR/ASR code is
wired and ready but **not executed** (paid Sarvam calls — user runs them).
**Next:** WP2 (chunk → embed → LanceDB).

## What was built

| File | Role |
|------|------|
| `kng/pipeline/extract/__init__.py` | Dispatch: one file → `ExtractedDoc`; never lets a bad file kill the batch; gates OCR/ASR via `use_ocr`/`use_asr` |
| `kng/pipeline/extract/documents.py` | `extract_doc` (docx + tables; legacy `.doc` via LibreOffice), `extract_pdf` (per-page text; OCRs scanned pages when enabled), `extract_pptx` (per-slide + notes), `extract_xlsx` (per-sheet) |
| `kng/pipeline/extract/media.py` | `extract_image` (Sarvam OCR + newspaper-clip cleanup), `extract_video` (Sarvam ASR, timestamped spans) |
| `kng/pipeline/normalize.py` | Unicode-script language detection (te/hi/en/mixed); optional English translation |
| `kng/pipeline/run.py` | Orchestrator CLI: discovery, incremental manifest, per-file `ExtractedDoc` JSON under `extracted/` |

## How to run

```bash
# Offline text pass (no paid calls) — already run:
python -m kng.pipeline.run --stage extract --no-ocr --no-asr

# Full pass incl. OCR (news clips + scanned PDFs) and ASR (videos) — needs SARVAM_API_KEY:
python -m kng.pipeline.run --stage extract          # add --translate for English fields

# Scope / control:
python -m kng.pipeline.run --stage extract --only "10_28.11.2024*"
python -m kng.pipeline.run --dry-run                # counts by type
```

Output: `extracted/<relative source path>.json` (one `ExtractedDoc` each) +
`index/manifest.json` (per-file, per-stage state). Re-running only reprocesses
new/changed files.

## Verification performed (offline)

- `--dry-run`: 645 processable files → source_doc 188, news_clip 353,
  press_release 63, video 25, slide 13, table 3.
- Full text pass: **583 processed, 1,640 segments, 2 errors**.
- Segments by type: source_doc 1424 (PDF pages), slide 167, press_release 61, table 3.
- Language mix: en 1286, te 347, mixed 4, unknown 18 — matches the corpus
  (English source evidence, Telugu press releases).
- Spot checks: SECI press release → 25,498 chars, `te`, meet_id 10, 2024-11-28;
  CERC regulations PDF → 2 pages, `en`, clean text. ✅

## Known gaps / TODOs

- **OCR/ASR not yet executed** — 430 files (353 images + 25 videos + some scanned
  PDF pages) currently have 0 segments. Run without `--no-ocr/--no-asr` to fill them.
- **Sarvam OCR job schema** (`doc-digitization/job/v1`) is defensive/best-effort;
  validate field names on the first live image and adjust `providers/ocr.py` if needed.
- **2 legacy `.doc` files** error out (need `soffice` on PATH). Install LibreOffice
  or convert them to `.docx` to include.
- Sarvam STT REST 30s cap handled by 25s ffmpeg chunking; for the longest videos
  consider Sarvam's batch STT later to cut call volume.

## What WP2 picks up

Read the `extracted/` JSON, chunk segments (~500–800 tokens, overlap, preserving
provenance), embed with the local multilingual model, and write to a portable
LanceDB store under `index/`. That makes plain RAG queryable end-to-end.
