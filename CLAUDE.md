# CLAUDE.md — KNG

Hybrid GraphRAG over the **YS Jagan press-meet archive** (~655 files / 584 MB,
Telugu-dominant + English + Hindi). A user asks a question → grounded synopsis
with **exact source citations**, plus cross-meet / temporal reasoning.

## Non-negotiables

- **Never run paid Sarvam calls during dev.** The user runs paid passes. Verify
  with local-only stages (`chunk`, `embed`, `query` are all free). This is the
  standing guardrail. Note `--local-only` is now *destructive* on this repo —
  see "Verification" below.
- **Sarvam key lives only in git-ignored `.env`.** Never commit it. `data/` is
  also git-ignored (not pushed).
- **Provider-first:** all models go through `kng/providers`. Sarvam is primary;
  local / other-cloud are `.env`-selectable fallbacks. No provider names hard-coded
  in pipeline logic.
- **Provenance-first:** every `Segment`/`Chunk` carries file + meet + date +
  page/slide/timestamp + publication + language, so citations resolve exactly.
- **Portable output:** all artifacts land under `index/` (+ `extracted/`) — no
  Docker, embedded stores (LanceDB vectors + NetworkX graph), copyable to the
  target system.
- **Incremental:** `kng/pipeline/manifest.py` tracks per-file content hash +
  per-stage status; re-running only processes new/changed files.

## Cluster → local hand-off

Data processing runs on the **cluster**; the query app runs **locally**.

- **Cluster:** WP0 (setup) → WP1/WP1b (extraction — Sarvam OCR/ASR/cleanup
  calls, ffmpeg chunking) → WP2 (chunk + local-embedding-model compute +
  LanceDB) → WP3 (graph build: entity/relation extraction, resolution,
  communities). End of WP3 = `index/` is complete and self-contained.
- **Local:** copy `index/` (+`extracted/`) after WP3, then build/run WP4
  (query engine) and WP5 (chat web UI) there — no bulk processing needed.
- WP6's `kng/pipeline/export.py` is the formal packaging step, but `index/`
  is already copyable as soon as WP3 finishes.

## Provider stack (locked)

Sarvam primary: LLM `sarvam-m`, ASR `saarika:v2.5`, OCR/Doc-Intelligence
`sarvam-vision`, translation `mayura:v1`. **Sarvam has no embeddings API** →
embeddings run locally on **`BAAI/bge-m3`** (needs `.[local]`; set via
`LOCAL_EMBED_MODEL`). Changed in WP2 from `multilingual-e5-base`, which
silently truncates at 512 tokens — bge-m3 allows 8192, so ~1000-token chunks
embed whole. Dim 1024.

## Work-package workflow

Build in numbered, independently-resumable **work packages (WPs)**. A WP is
"done" only when its code runs and is verified. End every WP with a **handover
doc** in `docs/handovers/` and update `README.md` + `docs/WORK_PACKAGES.md`.
Always record per-stage **document counts** in the handover.

See [docs/WORK_PACKAGES.md](docs/WORK_PACKAGES.md) for the WP tracker and
[docs/handovers/](docs/handovers/) for handovers. Approved plan:
`~/.claude/plans/act-as-a-software-snuggly-pond.md` (Revision 1 = Sarvam-first
universal extraction + progress tracking).

**Current resume point: WP3 (graph build).** Pipeline state as of 2026-07-25:

| stage | result |
|---|---|
| extract (WP1b) | 634/635 files · **2323 segments / 7.97M chars** · 651 paid calls · 1 known skip |
| chunk (WP2) | 635 files · **4267 chunks** · max 1394/8192 tokens |
| embed (WP2) | **4267 rows** in `index/lancedb` · `bge-m3`, 1024-dim |

Handovers: [WP1b](docs/handovers/WP1b-sarvam-revision.md) ·
[WP2](docs/handovers/WP2-index.md).

### Hard-won facts (don't rediscover these)

**Sarvam Document Intelligence:** rejects PDFs over 10 pages (`SarvamOCR` batches
them and renumbers pages); separates pages with a `---` rule, *not* a form feed;
embeds every figure as a base64 data URI — unstripped these were 89% of the
corpus. All three defects were silent, hidden by a bare `except`.

**Office-doc LLM cleanup fails on every call** (`cleanup=0` across 79 docs). Text
is intact via local parse; the Sarvam-first requirement is knowingly unmet there.
`cleanup_failed` is now tallied so it can't hide again.

**Free recovery commands:** `--rebuild-manifest` (rebuild a lost manifest from
`extracted/`) and `--repair-extracted` (strip base64, re-paginate OCR'd docs).

**Operational:** never run two extract passes concurrently — they double-bill and
race on `extracted/`. Long CPU stages get killed on the head node at ~2100% CPU;
embed survived at `OMP_NUM_THREADS=12`, but prefer a batch job for WP3.

**Embedding/chunking:** chunks are sized with the *model's own tokenizer*, never
a char heuristic, because e5-base silently truncated at 512. Chunk language is
detected per chunk, not inherited from its segment. Re-chunking is deterministic,
so metadata can be repaired without re-embedding.

## Layout

```
kng/config.py models.py stats.py query.py(WP2 retrieval smoke test)
kng/providers/    sarvam.py llm.py embeddings.py ocr.py asr.py translate.py
kng/pipeline/     manifest.py metadata.py normalize.py chunk.py embed.py graph_build.py run.py export.py
kng/pipeline/extract/  documents.py media.py
kng/store/        vector.py(LanceDB, WP2)  graph.py(WP3)
kng/retrieval/ generation/ api/           # WP4–WP5
config/ontology.yaml   docs/   extracted/
index/   manifest.json stats.json chunks/ lancedb/   # portable, copyable
```

## Verification (no paid calls)

```bash
source .venv/bin/activate
python -c "import kng, kng.config, kng.models, kng.providers, kng.pipeline.run, \
           kng.pipeline.chunk, kng.pipeline.embed, kng.store.vector, kng.query"   # imports clean
python -m kng.pipeline.run --stage chunk     # re-chunk, ~36s, deterministic
python -m kng.pipeline.run --stage embed     # local model; no-op once indexed
python -m kng.stats                          # per-stage doc counts (WP1b+)
python -m kng.query "Tirupati laddu" -k 5    # retrieval + citations (WP2+)
```

> ⚠️ **Do not run `--stage extract --local-only --force`.** It was the offline
> dev check *before* the paid pass; run now it overwrites every `extracted/`
> doc with local-parse output and destroys 651 calls' worth of OCR/ASR text.
> Extraction is already complete — to re-verify it, use `--rebuild-manifest`
> or `--repair-extracted`, both of which are read-mostly and free.
