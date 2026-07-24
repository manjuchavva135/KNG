# Handover — WP1b: Sarvam-first universal extraction + progress tracking (⏳ RESUME HERE)

**Status:** 📋 Approved, **not yet implemented**. This is the resume point.
Prompted by two user requirements after WP1:

1. **Every document must go through the Sarvam API — even text extraction**
   (not just images/video).
2. **Always track how many documents were processed per stage / work package.**

Design decisions confirmed with the user:
- Office docs (docx/pptx/xlsx): **local text → Sarvam LLM cleanup** (not
  LibreOffice→PDF). PDFs & images: **Sarvam Document Intelligence OCR**.
- **Sarvam is primary; local parse is fallback** (Sarvam fails / no key / `--local-only`).

See `Revision 1` in the approved plan: `~/.claude/plans/act-as-a-software-snuggly-pond.md`.

## To implement (exact next steps)

1. **`kng/stats.py`** (new) — per-stage counters persisted to `index/stats.json`;
   `set_stage(stage, counts)`, `render()`, `python -m kng.stats`. Counts:
   `{total, processed, skipped, errors, segments, by_type, sarvam_calls:{ocr,cleanup,asr,translate}}`.
   *(Write was interrupted; re-create it.)*
2. **`kng/models.py`** — add `sarvam_calls: dict[str,int] = {}` to `ExtractedDoc`
   so extractors report how many/what Sarvam calls they made.
3. **`kng/providers/llm.py`** — add `clean_document(text, lang_hint="") -> str`:
   Sarvam chat with a strict prompt — *clean & structure to markdown, preserve ALL
   content, do NOT summarise, keep the original language*.
4. **`kng/pipeline/extract/documents.py`**
   - `extract_pdf`: when `sarvam and use_ocr` → Sarvam OCR whole file (fallback to
     current PyMuPDF page text on failure). Else PyMuPDF (current behaviour).
   - `extract_doc/extract_pptx/extract_xlsx`: get local text (current), then if
     `sarvam and use_cleanup` → `clean_document()` becomes `text_original`
     (fallback raw local text).
   - Thread `sarvam`, `use_cleanup` params + count Sarvam calls into `sarvam_calls`.
5. **`kng/pipeline/extract/__init__.py`** — extend `extract_file(..., sarvam=True,
   use_cleanup=True)`; route params; aggregate `doc.sarvam_calls`.
6. **`kng/pipeline/run.py`** — new flags `--local-only` (sarvam=False, fully
   offline) and `--no-cleanup`; keep `--no-ocr/--no-asr`. Aggregate per-file
   `sarvam_calls` + `by_type` → `stats.set_stage("extract", …)` and print rollup
   (`extract: 583/645 done · 1640 seg · N sarvam calls`).
7. **Re-run** (user, with key): `python -m kng.pipeline.run --stage extract`
   to route the 645 files through Sarvam. Offline check first:
   `python -m kng.pipeline.run --stage extract --local-only --force`.
8. Update this handover → done; write `docs/handovers/WP2-index.md` next.

## Guardrails
- Do **not** auto-run paid Sarvam calls during dev — user runs the full pass.
  Verify with `--local-only`.
- `providers/ocr.py` Sarvam `doc-digitization` job schema is best-effort → the
  user validates it live on one image/PDF and we adjust field names if needed.

## Then WP2 (chunk → embed → LanceDB)
`kng/pipeline/chunk.py` (≈500–800 tok, overlap, keep provenance) →
`kng/pipeline/embed.py` (local `multilingual-e5-base`; needs `uv pip install -e
'.[local]'`) → `kng/store/vector.py` (LanceDB under `index/`) → `kng/query.py`
(retrieval smoke). Then plain RAG works end-to-end.
