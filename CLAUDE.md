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

**Current resume point: WP3's paid extraction pass, then WP4.** WP3 code is
complete and verified free-of-charge. Pipeline state as of 2026-07-25:

| stage | result |
|---|---|
| extract (WP1b) | 634/635 files · **2323 segments / 7.97M chars** · 651 paid calls · 1 known skip |
| chunk (WP2) | 635 files · **4267 chunks** · max 1394/8192 tokens |
| embed (WP2) | **4267 rows** in `index/lancedb` · `bge-m3`, 1024-dim |
| graph — structural (WP3, free) | **698 nodes · 714 edges · 21 communities** in `index/graph` |
| graph — LLM extraction (WP3, **in progress**) | **1043 / 4251 units** · 11,429 entities · 2,721 relations |

Resume extraction (re-bills nothing — the content-hash cache is the record):
`nohup .venv/bin/python -m kng.pipeline.run --stage graph --concurrency 12 > graph.log 2>&1 &`

Handovers: [WP1b](docs/handovers/WP1b-sarvam-revision.md) ·
[WP2](docs/handovers/WP2-index.md) · [WP3](docs/handovers/WP3-graph.md).

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

**Sarvam integration — six hard-won facts** (full detail in
[WP3 handover](docs/handovers/WP3-graph.md)):

1. **`sarvam-m` is deprecated** — the API 400s on it. This is the real cause of
   WP1b's `cleanup=0` across all 79 office docs; every LLM call had been failing.
   Use `sarvam-105b` (`sarvam-30b` failed the same probe).
2. **`reasoning_effort="low"`, not the `"medium"` default.** Measured: 44
   entities / 29 relations / 0 failures vs 25 / 4 / 1. Less thinking leaves more
   of the output cap for the answer. `null` disables reasoning but under-extracts
   ~6×.
3. **Starter tier caps output at 4096 tokens** — the reason chunks are split and
   truncated JSON is repaired rather than discarded.
4. **`tool_choice="required"` makes the model emit nothing**; use `"auto"`.
5. **Keep-alive connections go stale and hang forever** — this killed four runs.
   Every worker parks in `poll_schedule_timeout` on a socket that is
   `ESTABLISHED` locally but dead server-side, while a fresh connection answers
   in 1s. Fixed with `max_keepalive_connections=0` + `Connection: close`.
   Rate limiting made it *worse* by adding idle gaps.
6. **The SDK's timeouts never fire.** Chat goes through direct REST
   (`sarvam.chat_completion`), as OCR already did. Rate limit is **40 req/min
   per account** on Starter; `LLM_RPM` keeps us under it.

Also `_unwrap` ends in `return str(resp)`, so an unexpected shape becomes fake
"model output"; the graph path uses a strict variant.

Triage for "hung or just slow?":
`for t in $(ls /proc/<pid>/task); do cat /proc/<pid>/task/$t/wchan; echo; done | sort | uniq -c`

**Graph cost gate:** one DSC merit-list PDF is 598 chunks of `<td>` rows (a second
is 152). `graph_extract.select_chunks` trims 4267 chunks to 2950, then splitting
over-long chunks raises it to 4251 units. Skipped chunks stay searchable in
LanceDB.

**The extraction cache, not the manifest, decides paid work.** They desync — a
fixture run once marked all 635 files done and a real pass then extracted
nothing while reporting success. Cache entries are fingerprinted by
provider/model/`PROMPT_VERSION`; bump it when the prompt changes.

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
           kng.pipeline.chunk, kng.pipeline.embed, kng.pipeline.graph_build, \
           kng.pipeline.graph_extract, kng.store.vector, kng.store.graph, \
           kng.graph.ontology, kng.query, kng.graph_query"                       # imports clean
python -m kng.pipeline.run --stage chunk     # re-chunk, ~36s, deterministic
python -m kng.pipeline.run --stage embed     # local model; no-op once indexed
python -m kng.pipeline.run --stage graph --plan-only        # paid-pass cost report, 0 calls
python -m kng.pipeline.run --stage graph --structural-only  # free metadata graph
python -m kng.stats                          # per-stage doc counts (WP1b+)
python -m kng.query "Tirupati laddu" -k 5    # retrieval + citations (WP2+)
python -m kng.graph_query stats                                # graph (WP3+)
python -m kng.graph_query neighbors "10" --type PressMeet      # works pre-paid-pass
# Entity queries (`timeline "TTD"`, `neighbors "Jagan"`) need the paid pass —
# before it, the graph holds only meets/sources/publications/dates.
```

To exercise the *paid* graph path without spending, set `KNG_FAKE_LLM=1` — it
swaps in an offline fixture provider. Cache entries are fingerprinted by
provider/model/prompt version, so fixture output can never be mistaken for a paid
result. Delete `index/graph/extractions/` before a real run anyway.

> ⚠️ **Do not run `--stage extract --local-only --force`.** It was the offline
> dev check *before* the paid pass; run now it overwrites every `extracted/`
> doc with local-parse output and destroys 651 calls' worth of OCR/ASR text.
> Extraction is already complete — to re-verify it, use `--rebuild-manifest`
> or `--repair-extracted`, both of which are read-mostly and free.
