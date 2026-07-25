# Handover — WP2: chunk → embed → LanceDB

**Status:** ✅ **Done.** 2323 segments → **4267 chunks**, embedded with
`BAAI/bge-m3` (1024-dim) into `index/lancedb`. `python -m kng.query "..."`
returns ranked passages with exact citations, so plain RAG works end-to-end —
the WP2 exit criterion. **Zero paid API calls**: embeddings run locally.

Next: WP3 (knowledge graph).

## What was built

| file | role |
|---|---|
| `kng/pipeline/chunk.py` (new) | token-aware, structure-aware splitting → `index/chunks/<rel>.json` |
| `kng/store/vector.py` (new) | LanceDB table, per-file upsert, cosine search, FTS index |
| `kng/pipeline/embed.py` (new) | batch embed → LanceDB, resumable per file |
| `kng/query.py` (new) | retrieval smoke test with metadata prefilters |
| `kng/models.py` | `Chunk.content_hash` added (duplicate collapsing) |
| `kng/providers/embeddings.py` | exposes `max_tokens`; ST 5.x dim-API compatibility |

`run.py` needed no change — `_run_later_stage` already dispatched
`run_chunk(man, files)` / `run_embed(man, files)`.

## Decision: embedding model changed to `bge-m3`

The approved plan called for 500–800-token chunks on `multilingual-e5-base`.
**e5-base hard-truncates at 512 tokens**, so those chunks would have lost their
tails silently — the same failure class as the base64 and 10-page-cap defects in
WP1b. `BAAI/bge-m3` accepts 8192 tokens, is strongly multilingual, and keeps a
whole argument from a press release in one chunk.

Switching cost no code: the model id is env-driven (`LOCAL_EMBED_MODEL`), and
`LocalEmbedder` already applied the `query:`/`passage:` prefix only for e5 —
bge-m3 is trained without prefixes, so the existing check is correct as written.
`CLAUDE.md`'s locked-stack section is updated to match. **Dim changed 768 → 1024.**

Measured token density on this corpus under bge-m3's XLM-R vocabulary:
Telugu **4.6 chars/token**, English **5.5**. The tokenizer-free fallback
constants in `chunk.py` sit a little below these so estimates lean small. Tested
by forcing that branch: on atypical text it *can* exceed `CEILING_TOKENS`
(1413 vs 1400) — harmless, since the ceiling is our tuning knob and the real
limit (8192) keeps a wide margin. Install `.[local]` for exact counts.

## Chunking design

Budget: **target 1000 tokens, 120 overlap, hard ceiling 1400** (vs the model's
8192 — room to raise later without re-tuning the splitter).

The corpus is lopsided at both ends, and the two ends need opposite treatment:

- **Split**: press releases arrive as *one* segment per document (median 18,564
  chars, max **134,710**). Cuts are chosen on structure — Markdown headings →
  blank-line paragraphs → sentence ends (`.?!` plus danda `।॥`) → hard token
  slice. Sarvam OCR emits real `##` headings, so cuts land on real sections.
- **Merge**: video ASR spans arrive one utterance at a time (median 268 chars).
  Consecutive spans fuse into ~1000-token chunks carrying the true `start–end`
  timestamp range.

**Pages and slides are never merged.** A chunk spanning `p.4`–`p.5` could not
cite either exactly, so segments are only ever split *within* one locator. The
cost is 35 chunks under 20 tokens (title slides, PDF cover pages) — real,
citable units, kept deliberately.

Two refinements found by inspecting output rather than trusting the first draft:

1. Preferring the *latest* structural break pinned every chunk to the 1400
   ceiling (mean 1347). Breaks are now scored by `(priority, -distance from
   target)`, giving a median of exactly 1000.
2. Rewinding by raw token count started overlaps mid-word (tokens are subwords).
   The overlap start now snaps back to a sentence boundary.

## Duplicate content

The archive holds **14 groups of byte-identical documents and 125 duplicate
segments (315,078 chars, 4%)** — e.g. `4.CBN_TankersUSE_22092024.mp4` appears 3×.
Every copy stays indexed, because each source file must remain citable. Instead
each chunk carries `content_hash` (sha1 of whitespace-normalised text) and
`kng/query.py` collapses duplicates at retrieval time, over-fetching 3× so
dedup still fills `k`. Turn it off with `--no-dedup`.

## Storage

`index/lancedb`, table `chunks`, explicit pyarrow schema (never inferred, so a
re-embed cannot silently change column types). Every `Chunk` provenance field is
a real column, which is what makes the prefilters below possible.

- `upsert_file()` deletes by `source_file` then inserts → re-embedding one
  changed file is idempotent.
- Search uses **cosine**, not LanceDB's default L2. Ranking is identical for
  normalised vectors, but `_distance` then reads as `1 - similarity`.
- A **full-text index** on `text` is built at the end of the embed stage, so
  WP4's keyword leg of hybrid retrieval is ready without re-scanning the table.

Prefilters available now: `--lang`, `--source-type`, `--meet`, `--since`,
`--until` — the basis for the cross-meet and temporal questions the project
targets (98% of segments carry a date).

## Verification (all offline, no paid calls)

```bash
source .venv/bin/activate
uv pip install -e '.[local]'                  # torch + sentence-transformers
python -m kng.pipeline.run --stage chunk      # 36s
python -m kng.pipeline.run --stage embed      # ~55 min CPU total, resumable
python -m kng.stats
python -m kng.query "What did Jagan say about the Tirupati laddu?"
```

Asserted explicitly, because WP1b's defects were all *silent*:

| check | result |
|---|---|
| No truncation — max chunk tokens vs model limit | **1394 / 8192** ✅ |
| No text lost — segment chars vs chunk chars | 7,967,484 → 8,675,968 (+8.9% overlap) ✅ |
| Every segment with text produced ≥1 chunk | 0 failures ✅ |
| Every chunk has a citation | 0 missing ✅ |
| Locators survive | page=2995 slide=167 video=25 ✅ |
| Page provenance spot-check (20-page PDF) | 20 chunks, pages 1–20 ✅ |
| Round-trip — chunks on disk vs LanceDB rows | 4267 == 4267 ✅ |
| Vectors — dim, nulls, normalisation | 1024, 0 nulls, all norms 1.0000 ✅ |
| Re-running `--stage embed` | "nothing to do" ✅ |

Retrieval, live:

- **Telugu** `"ఏపీ మద్యం కుంభకోణం"` → Sakshi clip on liquor kickbacks (0.553),
  distributor permissions table (0.528), matching press release (0.522).
- **English → Telugu evidence** `"What did Jagan say about the Tirupati laddu
  ghee adulteration?"` → laddu-issue address (0.659), ThePrint adulterated-ghee
  report (0.637), Telugu SC/SIT press release (0.624). **The cross-lingual bet
  paid off** — no translation pass needed.
- **Prefilter** `--lang te --since 2025-01-01` returned only Telugu rows dated
  2025+.

Token spread: min 4, median 854, mean 724, max 1394.

Chunks by type: `source_doc=2995 news_clip=567 press_release=507 slide=167
video=25 table=6`.

## Defects found during the run

**1. The embed job was killed at 67%** (doc 425/633, 3528 rows) with no
traceback, no error and no OOM — memory peaked at 4.3 GB against 376 GB, and no
cgroup limit is set. The likely cause is a head-node watchdog: the process held
**2100% CPU across 255 threads for 40 minutes** on a login node. Unproven —
`dmesg` is not readable here.

*Nothing was lost.* Embed commits per source file and checkpoints the manifest
every 25, so resuming re-did only the tail. The resumed run used
`OMP_NUM_THREADS=12` (≈1000% CPU instead of 2100%) and completed. **If a long
stage is killed again, submit it as a batch job rather than running on the head
node** — WP3's graph build will have the same profile.

**2. Chunks inherited their segment's language** instead of detecting their own.
Long segments are code-switched, so an English passage inside a Telugu-labelled
document answered a `--lang te` filter. **118 of 4267 chunks (2.8%)** were
mislabelled. `_to_chunk` now calls `detect_language` on the chunk text.

Fixing this needed **no re-embedding**: only metadata changed. Re-chunking is
deterministic (same 4267 ids, same max 1394 tokens), so the fix was applied by
re-chunking, asserting every chunk's text was byte-identical to the indexed row
— which is what makes the stored vectors still valid — then rewriting the table
with the corrected column and rebuilding the FTS index. Distribution moved
`en 2547→2558, te 1534→1521, mixed 180→168, hi 6→4, unknown 0→16`.

**3. Stats under-reported the index after a resume.** `processed`/`skipped` are
per-run by convention, so the `embed` line described only its own tail — and
worse, re-running the stage as a no-op *overwrote* the record with zeroes.
`run_embed` now calls `_describe_index()` on both paths, recording `rows` and
`indexed_by_type` from the table itself, so the reported index size is true
regardless of how many resumes it took. `kng.stats` prints
`indexed: N rows in the vector store` and shows chunk counts rather than segments.

## Notes for WP3/WP4

- `index/` is now self-contained and copyable: `manifest.json`, `stats.json`,
  `chunks/`, `lancedb/`.
- Re-chunking is free (36s) and re-embedding is ~55 min of local CPU — so chunk
  size can be re-tuned in WP4 if retrieval proves too coarse or too fine.
- **Cross-lingual retrieval works** on the smoke tests above — English questions
  did retrieve Telugu evidence at 0.62–0.66, so the decision not to pay for a
  Telugu→English translation pass holds. This is anecdotal, not an eval: WP4
  should measure recall over a real question set. If it disappoints there, the
  translation pass (~980 segments via `mayura:v1`) remains the fallback, and
  `Chunk.text_en` already exists to hold the result.
- No GPU on the head node (48 cores / 376 GB RAM). If the cluster objects to
  long CPU jobs there, run `--stage embed` as a batch job.
