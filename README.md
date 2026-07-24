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
- **Portable output:** all artifacts land in a self-contained `index/` directory
  you can copy to another system after embedding.
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

| Type | Count | Handling |
|------|-------|----------|
| jpeg/jpg/png | ~355 | Sarvam OCR (news clips) |
| pdf | 188 | text extract; OCR if scanned |
| docx/doc | 70 | authoritative transcripts |
| mp4 | 25 | Sarvam ASR (chunked, timestamped) |
| pptx | 15 | slide + notes text |
| xlsx | 3 | tables → markdown |

---

## Setup

```bash
uv venv --python 3.11 .venv
uv pip install -e .            # core (extraction + vector store)
uv pip install -e '.[local]'  # local embeddings + whisper fallback
uv pip install -e '.[cloud]'  # optional cloud providers (anthropic/cohere/neo4j)
uv pip install -e '.[api]'    # FastAPI backend

cp .env.example .env          # then fill in SARVAM_API_KEY
```

Requires `ffmpeg` on PATH (audio extraction for ASR).

---

## Usage

```bash
# 1. Ingest (incremental — safe to re-run; only new/changed files are processed)
python -m kng.pipeline.run --stage all           # extract → normalize → chunk → embed → graph
python -m kng.pipeline.run --stage extract       # or run one stage
python -m kng.pipeline.run --only "10_28.11.2024*"  # limit to matching press meet(s)

# 2. Ask a question (cited synopsis)
python -m kng.query "Summarise YS Jagan's allegations on the SECI power deal"

# 3. Serve the chat web app
uvicorn kng.api.main:app --reload                # http://localhost:8000

# 4. Export the portable index for your other system
python -m kng.pipeline.export --out kng_index.tar.gz
```

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
    normalize.py chunk.py embed.py graph_build.py run.py export.py
  store/               # vector.py (LanceDB) · graph.py (NetworkX/Neo4j)
  retrieval/  generation/  api/   # WP4 / WP5
config/  ontology.yaml            # graph node/edge types + alias table
docs/                            # WORK_PACKAGES.md + handovers/
index/                           # portable output (vectors, graph, manifest)
```

---

## Work packages

Built in independently-resumable work packages; each ends with a handover doc in
[`docs/handovers/`](docs/handovers/). See [docs/WORK_PACKAGES.md](docs/WORK_PACKAGES.md).

| WP | Scope | Status |
|----|-------|--------|
| WP0 | Foundation: scaffold, config, data model, manifest, providers | ✅ done |
| WP1 | Multimodal extraction + Sarvam OCR/ASR + normalization | ✅ text done · OCR/ASR ready |
| WP1b | Sarvam-first universal extraction + per-stage doc counts | 📋 approved — **resume here** |
| WP2 | Chunk → embed → LanceDB (RAG works) | ⏳ |
| WP3 | Knowledge graph build | ⏳ |
| WP4 | GraphRAG query engine (cited synopsis) | ⏳ |
| WP5 | FastAPI + chat web UI | ⏳ |
| WP6 | Eval, hardening, portable export | ⏳ |

> **Resume point:** [`docs/handovers/WP1b-sarvam-revision.md`](docs/handovers/WP1b-sarvam-revision.md)
> has the exact next steps. `data/` is git-ignored (not pushed to GitHub).
