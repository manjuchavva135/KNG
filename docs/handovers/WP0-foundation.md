# Handover — WP0: Foundation

**Status:** ✅ Done & verified. **Next:** WP1 (multimodal extraction).

## What was built

The project skeleton and the seams everything else plugs into.

| Area | File(s) | What it does |
|------|---------|--------------|
| Packaging | `pyproject.toml` | uv/hatchling project; extras `local`, `cloud`, `api` |
| Secrets | `.env` (git-ignored), `.env.example`, `.gitignore` | Sarvam key + provider selection; key never committed |
| Config | `kng/config.py` | One typed `Settings` object read from `.env`; path helpers |
| Data model | `kng/models.py` | `SourceType`, `Locator`, `Segment`, `ExtractedDoc`, `Chunk` — provenance-first; `Segment.citation_label()` renders a short citation |
| Manifest | `kng/pipeline/manifest.py` | Content-hash (sha1) per-file, per-stage status → incremental ingest |
| Path metadata | `kng/pipeline/metadata.py` | Derives meet id / date / topic / publication / source_type from folder & file names |
| Ontology | `config/ontology.yaml` | Graph node/edge types + alias seed table for entity resolution |
| Providers | `kng/providers/` | Factories + implementations for LLM, embeddings, OCR, ASR, translate |
| Docs | `README.md`, `docs/WORK_PACKAGES.md` | Setup, usage, WP tracker |

### Provider layer (the modular seam)

`kng/providers/__init__.py` exposes cached factories: `get_llm`, `get_embedder`,
`get_ocr`, `get_asr`, `get_translator`. Each picks an implementation from `.env`:

- **LLM** — `SarvamLLM` (sarvam-m via `sarvamai` SDK) · `AnthropicLLM` (optional).
- **Embeddings** — `LocalEmbedder` (sentence-transformers `multilingual-e5-base`,
  handles the e5 `query:`/`passage:` prefixes) · `CohereEmbedder` (optional).
  *Sarvam has no embeddings API — this is deliberate.*
- **OCR** — `SarvamOCR` (Document Intelligence `sarvam-vision`, REST job API) ·
  `TesseractOCR` (`tel+hin+eng`) · `NoOCR`.
- **ASR** — `SarvamASR` (ffmpeg → 25s chunks → `saarika`, tracks timestamps) ·
  `WhisperASR` (faster-whisper) · `NoASR`.
- **Translate** — `SarvamTranslator` (`mayura`) · `NoTranslator`.

## Key decisions & trade-offs

1. **Sarvam-first, `.env`-swappable.** Primary provider for LLM/ASR/OCR/translate.
   No provider names are hard-coded in pipeline logic.
2. **Embedded, portable stores (no Docker).** The environment has no Docker and
   no cloud keys besides Sarvam, and the user copies the index to another system,
   so we default to LanceDB (vectors) + NetworkX (graph) under `index/`. Neo4j is
   supported when `NEO4J_URI` is set. *(Deviation from the plan's Neo4j default,
   forced by no-Docker + portability requirement.)*
3. **Local embeddings for reproducibility.** A fixed local model id embeds
   identically on the target system.
4. **Provenance-first data model.** Locator carries page / slide / video span so
   citations resolve exactly to where text came from.

## Verification performed

- `uv pip install -e .` succeeds (core deps incl. `sarvamai==0.1.28`, LanceDB,
  PyMuPDF, python-docx/pptx, openpyxl, requests).
- Package imports clean: `kng`, `config`, `models`, `providers`, `pipeline.*`.
- `settings()` reads `.env` correctly (Sarvam key present; providers = sarvam/local).
- `metadata.derive(...)` on a real file →
  `id=10, date=2024-11-28, topic="SECI – POWER SECTOR", type=press_release`. ✅

## Known gaps / TODOs (picked up later)

- **Sarvam OCR job schema is best-effort** — the exact `doc-digitization` request/
  response is validated live in WP1 (defensive code handles sync-text and
  async-job shapes; adjust field names once probed).
- Legacy `.doc` (2 files) has no extractor yet — WP1 adds a LibreOffice/soffice
  best-effort path or skips with a logged error.
- Local extras (`sentence-transformers`, `faster-whisper`) not installed yet —
  WP2 installs `.[local]` for embeddings.

## What WP1 picks up

Build `kng/pipeline/extract/` (documents + media), `normalize.py`, and the
`run.py` orchestrator; wire OCR/ASR through the WP0 providers; validate Sarvam
OCR/ASR/chat **live** on one sample each; then run extraction across the corpus
and write `ExtractedDoc` JSON to `extracted/`, updating the manifest.
