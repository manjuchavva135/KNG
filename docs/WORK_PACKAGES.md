# KNG — Work Packages

Work is built in independently-resumable **work packages (WPs)**. Each WP ends
with a **handover doc** in `docs/handovers/` and an update to the top-level
`README.md`. A WP is "done" only when its code runs and is verified.

| WP | Scope | Handover | Status |
|----|-------|----------|--------|
| WP0 | Foundation: scaffold, config/secrets, data model, manifest, provider abstraction | [WP0-foundation.md](handovers/WP0-foundation.md) | ✅ done |
| WP1 | Multimodal extraction: docx/pdf/pptx/xlsx + Sarvam OCR (news clips) + Sarvam ASR (videos) + normalization | [WP1-extraction.md](handovers/WP1-extraction.md) | ✅ text done · OCR/ASR ready |
| WP1b | Sarvam-first universal extraction (all docs → Sarvam) + per-stage document counts | [WP1b-sarvam-revision.md](handovers/WP1b-sarvam-revision.md) | ✅ done · 2026-07-25: 634/635 files · 2323 seg · 7.97M chars · 651 calls · 1 skip (legacy Telugu font) 
| WP2 | Index: chunk → embed → LanceDB vector store (plain RAG works end-to-end) | [WP2-index.md](handovers/WP2-index.md) | ✅ done · 2323 seg → 4267 chunks · bge-m3 (1024d) |
| WP3 | Knowledge graph: LLM entity/relation extraction → resolution → graph store | [WP3-graph.md](handovers/WP3-graph.md) | ✅ done · 2026-07-26: full corpus extracted, **4251/4251 units · 1 unrecoverable failure** → **8120 nodes / 10773 edges / 1157 communities** across all 33 meets |
| WP4 | GraphRAG query engine: hybrid retrieval (vector+keyword+graph) → cited synopsis | [WP4-query.md](handovers/WP4-query.md) | ✅ done · 2026-07-25: RRF over 4267 chunks + graph facts · citation verification · 20 tests · cold 13.8 s / warm 0.22 s per query |
| WP5 | FastAPI backend + chat web UI with citations & source viewer | [WP5-app.md](handovers/WP5-app.md) | ✅ done · 2026-07-26: **PressMeets RAG** — auth (scrypt + signed cookies), token-streaming SSE answers, clickable citations opening the exact passage, history, admin stats · 57 tests · verified against the real API (352 deltas / 17 s, 0 invalid citations) |
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
