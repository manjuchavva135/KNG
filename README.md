# KNG — Hybrid GraphRAG over the YS Jagan Press-Meet Archive

Turn ~655 files (584 MB) of multilingual political media — Telugu-dominant, plus
English and Hindi — into a system that answers a question with a **clear,
grounded synopsis and exact source citations**, and can **trace an issue, person,
or scheme across press meets over time**.

- **Architecture:** hybrid GraphRAG = vector RAG (semantic synopsis) + a knowledge
  graph (cross-meet / temporal reasoning). Every answer cites its sources.
- **Provider-first & modular:** Sarvam powers LLM inference, ASR, OCR and
  translation; embeddings run on a local multilingual model. Swapping any
  provider is a `.env` change, not a code change.
- **Clone-and-run:** `index/` and `extracted/` are committed (~204 MB), so a
  clone is a working system — no artifact transfer, no reprocessing, and **no
  API key** for querying. Only the 584 MB `/data/` source archive stays out.
- **Scalable:** a content-hash manifest means dropping new press meets into
  `data/` only reprocesses what changed.

> Corpus is politically sensitive. The pipeline never fabricates attributions —
> answers are grounded in retrieved chunks or it says it doesn't know.

---

## Data

```
data/
  YS JAGAN_PRESSMEETS DATA/<id>_<DD.MM.YYYY>_<TOPIC>/   # ~27 dated press meets
  July 2026/<TOPIC>/                                    # topical set
```

635 files are ingestible (10 `~BROMIUM` sandbox stubs are excluded as
undecodable). Extracted: **2323 segments / 7.97M chars → 4267 chunks.**

| Type | Files | Chunks | Handling |
|------|------:|-------:|----------|
| jpeg/jpg/png | 343 | 567 | Sarvam OCR (news clips) |
| pdf | 188 | 2995 | Sarvam OCR, batched ≤10 pages; PyMuPDF fallback |
| docx/doc | 63 | 507 | authoritative transcripts |
| mp4 | 25 | 25 | Sarvam ASR — spans merged, timestamps kept |
| pptx | 13 | 167 | slide + notes text |
| xlsx | 3 | 6 | tables → markdown |

Languages by chunk: `en 2558 · te 1521 · mixed 168 · unknown 16 · hi 4`.
98% carry a date, enabling temporal filtering. One file is knowingly skipped —
an RTF in a legacy non-Unicode Telugu font (Shree-Lipi), which would index as
meaningless bytes.

---

## Setup

### Querying an existing index (no API key needed)

The committed `index/` + `extracted/` make a fresh clone immediately queryable:

```bash
git clone <repo> KNG && cd KNG
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e '.[local]'   # bge-m3 + torch (~2GB), for query-side embedding
cp .env.example .env           # SARVAM_API_KEY only needed to re-run the pipeline

python -m kng.stats            # per-stage document counts
python -m kng.graph_query stats
python -m kng.query "Tirupati laddu" -k 5
```

The first query downloads the bge-m3 model from HuggingFace (~2 GB, free). After
that everything runs offline.

### Re-running the pipeline

Needs the `/data/` source archive (not in git) and a `SARVAM_API_KEY`:

```bash
uv pip install -e .            # core (extraction + vector store)
uv pip install -e '.[cloud]'   # optional providers (anthropic/cohere/neo4j)
uv pip install -e '.[api]'     # FastAPI backend
```

`.[local]` is not optional: Sarvam has no embeddings API, so the index is built
by a local model (`LOCAL_EMBED_MODEL`, default `BAAI/bge-m3`, 1024-dim).

Requires `ffmpeg` on PATH (audio extraction for ASR). LibreOffice (`soffice`) is
used for legacy `.doc` when present, but both RTF and OLE2 `.doc` have
pure-Python fallbacks, so it is not required.

---

## Usage

```bash
# 1. Ingest (incremental — safe to re-run; only new/changed files are processed)
python -m kng.pipeline.run --stage all           # extract → normalize → chunk → embed → graph
python -m kng.pipeline.run --stage chunk         # or run one stage
python -m kng.pipeline.run --only "10_28.11.2024*"  # limit to matching press meet(s)
python -m kng.stats                              # per-stage document counts

# 2. Search the index — ranked passages with exact citations (WP2)
python -m kng.query "Tirupati laddu ghee adulteration"
python -m kng.query "ఏపీ మద్యం కుంభకోణం" -k 5
python -m kng.query "liquor scam" --lang te --since 2025-01-01   # metadata prefilters

# 3. Ask a question — grounded synopsis with [n] citations (WP4)
python -m kng.answer "What did Jagan say about the Tirupati laddu adulteration?"
python -m kng.answer "SECI solar tariff" -k 12 --since 2024-01-01
python -m kng.answer "TTD laddu" --retrieval-only        # free: the evidence, no LLM call
KNG_FAKE_LLM=1 python -m kng.answer "TTD laddu"          # free: whole path, offline fixture

# 4. Serve the chat web app                      (WP5, not built yet)
uvicorn kng.api.main:app --reload                # http://localhost:8000

# 5. Export the portable index for your other system   (WP6, not built yet)
python -m kng.pipeline.export --out kng_index.tar.gz
```

`kng.query` is retrieval only — it returns the evidence and where it came from.
`kng.answer` adds the graph leg and writes the cited synopsis: retrieval is free
and local, and the single synthesis call is the only paid step, so
`--retrieval-only` shows exactly what would be sent before anything is spent.
Citations are verified after generation — any `[n]` pointing at no source is
stripped and reported.

**Extraction is already complete on this checkout**; re-running `--stage extract`
costs paid Sarvam calls. `--stage chunk`/`embed`/`query` are free and local.

**Recovering the index without re-paying:** `--rebuild-manifest` reconstructs
lost incremental state from `extracted/`; `--repair-extracted` re-cleans the
extracted docs in place.

---

## Layout

```
kng/
  config.py            # typed settings from .env
  models.py            # Segment / ExtractedDoc / Chunk (provenance-first)
  providers/           # swappable model backends
    sarvam.py  llm.py  embeddings.py  ocr.py  asr.py  translate.py
  pipeline/
    manifest.py        # content-hash incremental state
    metadata.py        # derive meet id/date/topic/publication from paths
    extract/           # docx pdf pptx xlsx image(OCR) video(ASR)
    normalize.py chunk.py embed.py run.py export.py
    graph_build.py       # WP3 phases A (structural, free) + D (communities)
    graph_extract.py     # WP3 phases B/C/E — the paid ones
  graph/  ontology.py    # reads config/ontology.yaml; constrains + validates
  query.py               # vector retrieval smoke test (WP2)
  graph_query.py         # graph smoke test (WP3): neighbors/path/timeline
  answer.py              # WP4 CLI: hybrid retrieval → cited synopsis
  store/                 # vector.py (LanceDB) · graph.py (NetworkX/Neo4j)
  retrieval/             # WP4: hybrid.py (vector+BM25+RRF) · graph_context.py
  generation/            # WP4: synthesize.py (grounded prompt + citation check)
  api/                   # WP5
tests/  test_wp4.py                # python -m unittest discover -s tests
config/  ontology.yaml            # graph node/edge types + alias table
docs/                            # WORK_PACKAGES.md + handovers/
index/                           # portable output — copy this to the query machine
  manifest.json  stats.json  chunks/  lancedb/  graph/
```

---

## Work packages

Built in independently-resumable work packages; each ends with a handover doc in
[`docs/handovers/`](docs/handovers/). See [docs/WORK_PACKAGES.md](docs/WORK_PACKAGES.md).

| WP | Scope | Status |
|----|-------|--------|
| WP0 | Foundation: scaffold, config, data model, manifest, providers | ✅ done |
| WP1 | Multimodal extraction + Sarvam OCR/ASR + normalization | ✅ text done · OCR/ASR ready |
| WP1b | Sarvam-first universal extraction + per-stage doc counts | ✅ done · 634/635 files · 2323 seg |
| WP2 | Chunk → embed → LanceDB (RAG works) | ✅ done · 4267 chunks · bge-m3 (1024d) |
| WP3 | Knowledge graph build | ✅ done · 8120 nodes / 10773 edges / 1157 communities · full corpus, all 33 meets |
| WP4 | GraphRAG query engine (cited synopsis) | ✅ done · hybrid RRF + graph leg · 34 tests · warm 0.22 s/query |
| WP5 | FastAPI + chat web UI | ⏳ |
| WP6 | Eval, hardening, portable export | ⏳ |

> **Resume point:** WP5. WP3's extraction is fully complete (4251/4251 units) —
> entity, timeline and cross-meet questions work over the whole archive with no
> paid work remaining. `data/` is git-ignored (not pushed to GitHub).
