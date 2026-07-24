# KNG — Work Packages

Work is built in independently-resumable **work packages (WPs)**. Each WP ends
with a **handover doc** in `docs/handovers/` and an update to the top-level
`README.md`. A WP is "done" only when its code runs and is verified.

| WP | Scope | Handover | Status |
|----|-------|----------|--------|
| WP0 | Foundation: scaffold, config/secrets, data model, manifest, provider abstraction | [WP0-foundation.md](handovers/WP0-foundation.md) | ✅ done |
| WP1 | Multimodal extraction: docx/pdf/pptx/xlsx + Sarvam OCR (news clips) + Sarvam ASR (videos) + normalization | [WP1-extraction.md](handovers/WP1-extraction.md) | ✅ text done · OCR/ASR ready |
| WP1b | Sarvam-first universal extraction (all docs → Sarvam) + per-stage document counts | [WP1b-sarvam-revision.md](handovers/WP1b-sarvam-revision.md) | 📋 approved — **resume here** |
| WP2 | Index: chunk → embed → LanceDB vector store (plain RAG works end-to-end) | — | ⏳ |
| WP3 | Knowledge graph: LLM entity/relation extraction → resolution → graph store | — | ⏳ |
| WP4 | GraphRAG query engine: hybrid retrieval (vector+keyword+graph) → cited synopsis | — | ⏳ |
| WP5 | FastAPI backend + chat web UI with citations & source viewer | — | ⏳ |
| WP6 | Eval harness, hardening, portable export for the target system | — | ⏳ |

## Conventions

- **Provider-first:** all models go through `kng/providers`. Sarvam is primary;
  local/other-cloud are `.env`-selectable fallbacks.
- **Provenance-first:** every `Segment`/`Chunk` carries file + meet + date +
  page/slide/timestamp + publication + language, so citations are exact.
- **Incremental:** `kng/pipeline/manifest.py` tracks per-file content hash and
  per-stage status; re-running only processes new/changed files.
- **Portable:** all outputs live under `index/` (+ `extracted/`) and can be
  tarred and moved to another system.

## Handover doc template

Each handover records: what was built, how to run it, key decisions & trade-offs,
verification performed, known gaps / TODOs, and what the next WP should pick up.
