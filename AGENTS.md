# AGENTS.md — KNG

This file is the operating handover for coding agents working in this repository.
Read it before changing code. It consolidates the current implementation,
`CLAUDE.md`, the approved plan, and the work-package handovers.

## Mission

KNG is a multilingual, multimodal hybrid GraphRAG system over the YS Jagan
press-meet archive. The corpus is Telugu-dominant, with English and Hindi
material, and includes office documents, PDFs, newspaper images, presentations,
spreadsheets, and video/audio.

The final product must answer questions with:

- a clear synopsis grounded only in retrieved evidence;
- exact, resolvable source citations;
- cross-meet, entity, issue, and temporal reasoning;
- support for the user's query language; and
- an explicit low-confidence refusal instead of fabricated attribution.

The architecture combines vector retrieval for semantic synthesis with a
knowledge graph for relationships and timelines.

## Source-of-truth order

When documents disagree, use this order:

1. The user's current request.
2. The newest handover in `docs/handovers/`.
3. The current code and its verified behavior.
4. `docs/WORK_PACKAGES.md` and `README.md`.
5. `CLAUDE.md`.
6. The approved plan at
   `~/.claude/plans/act-as-a-software-snuggly-pond.md`.

The plan's revisions supersede its original cloud/Neo4j/Docker defaults:

- **Revision 1:** Sarvam-first universal extraction, local multilingual
  embeddings, LanceDB, NetworkX, portable artifacts, and persisted counts.
- **Revision 2:** run bulk data work through WP3 on the cluster, then transfer
  the completed artifacts to the local machine for WP4 and WP5.

There is no Docker requirement in the locked current direction.

## Non-negotiable guardrails

- **Never make paid Sarvam calls during development or verification.** The user
  runs paid ingestion. Until WP1b adds `--local-only`, the safe extraction flags
  are `--no-ocr --no-asr`; also do not enable translation. After WP1b, verify
  with `--local-only`.
- Never expose, print, edit casually, or commit `.env` or API keys. Use
  `.env.example` for documented configuration.
- Do not commit the raw corpus in `data/`, extracted output in `extracted/`, or
  generated indexes in `index/`. They are intentionally git-ignored.
- Keep all model/API use behind `kng/providers`. Pipeline, retrieval, and
  generation code must not hard-code a provider.
- Preserve original-language source text. Cleanup must not summarize, translate,
  omit, or invent content. English translation belongs only in `text_en`.
- Provenance is mandatory. Every segment and derived chunk must retain the source
  file, meet ID/title/date/topic, source type, publication, language, speaker
  where known, and page/slide/timestamp/paragraph locator.
- Generated claims must be traceable to evidence. Never fabricate political
  statements or attributions.
- Keep processing incremental through `kng/pipeline/manifest.py`; do not
  introduce a full-rebuild requirement for new or changed files.
- Keep outputs portable under `index/` and `extracted/`. Do not require a server
  or Docker for the default path.

## Current state and resume point

WP0 and the offline portion of WP1 are implemented. **Resume at WP1b.**

| WP | State | Notes |
|---|---|---|
| WP0 | Done and verified | Config, models, manifest, metadata, providers, ontology |
| WP1 | Text pass done | OCR/ASR wired but paid pass not run; 2 legacy `.doc` errors |
| WP1b | Approved, not implemented | Universal Sarvam extraction and persisted counts |
| WP2 | Not implemented | Chunk, embed, LanceDB, plain RAG |
| WP3 | Not implemented | Entity/relation extraction and graph |
| WP4 | Not implemented | Hybrid GraphRAG query and cited generation |
| WP5 | Not implemented | FastAPI and chat UI |
| WP6 | Not implemented | Evaluation, hardening, portable export |

Do not infer completion from planned filenames in README or `CLAUDE.md`.
At the time this file was created:

- `kng/stats.py` is absent;
- `ExtractedDoc` has no `sarvam_calls` field;
- `SarvamLLM` has no `clean_document` method;
- the CLI has no `--local-only` or `--no-cleanup` option;
- PDFs use local PyMuPDF text and OCR only pages judged scanned;
- DOCX/PPTX/XLSX extraction is local and has no Sarvam cleanup;
- chunk/embed/graph dispatch exists, but their modules do not;
- there is no test suite yet.

The exact WP1b checklist is in
`docs/handovers/WP1b-sarvam-revision.md`. Keep that handover and the tracker
updated as implementation advances.

## Cluster-to-local deployment boundary

Development and execution are deliberately split:

| Environment | Work packages | Responsibility |
|---|---|---|
| Cluster | WP0, WP1/WP1b, WP2, WP3 | Extraction, paid Sarvam batch calls, ffmpeg processing, local embedding-model compute, LanceDB creation, and graph construction |
| Local machine | WP4, WP5 | Hybrid retrieval, cited answer generation, FastAPI, and the interactive chat UI |
| Either | WP6 | Evaluation, hardening, and formal export packaging |

WP3 is the hand-off point. By its end, `index/` must be self-contained and
include the manifest, stats, vectors, and graph required for querying. Copy
`index/` **and** `extracted/` to the local machine so citation/source viewing can
resolve the underlying extracted evidence.

Code written in WP0-WP3 must be resumable and suitable for non-interactive
cluster runs. Code written in WP4-WP5 must not silently depend on the original
cluster, raw `data/`, cluster-only absolute paths, or a bulk re-ingestion step.
Store portable project-relative paths in artifacts.

`kng/pipeline/export.py` is planned for WP6 as the formal packaging command, but
the artifacts must already be directly copyable after WP3. Before hand-off,
verify that the copied index opens on a different filesystem location and that
sample citations still resolve against the copied `extracted/` tree.

## Locked provider and storage choices

- LLM: Sarvam `sarvam-m`
- OCR/document intelligence: Sarvam `sarvam-vision`
- ASR: Sarvam `saarika:v2.5`
- Translation: Sarvam `mayura:v1`
- Embeddings: local `intfloat/multilingual-e5-base`
- Vector store: embedded LanceDB under `index/`
- Graph store: NetworkX under `index/`
- Optional configured fallbacks: Anthropic, Cohere, Tesseract, faster-whisper,
  and Neo4j

Sarvam has no embeddings API. Do not try to route embeddings through Sarvam.
Provider selection comes from `kng/config.py` and `.env`.

## Repository map

```text
kng/
  config.py                 typed settings and project paths
  models.py                 provenance-first Segment/ExtractedDoc/Chunk models
  providers/                LLM, embedding, OCR, ASR, translation adapters
  pipeline/
    manifest.py             content-hash, per-file/per-stage incremental state
    metadata.py             path-derived meet/date/topic/publication/type
    normalize.py            script-based language detection and translation
    run.py                  ingestion CLI and future stage dispatch
    extract/
      documents.py          DOC/DOCX, PDF, PPTX, XLSX extraction
      media.py              image OCR and timestamped video/audio ASR
config/ontology.yaml        graph node/edge types and alias seeds
docs/WORK_PACKAGES.md       WP tracker and workflow
docs/handovers/             resumable WP implementation records
data/                       private raw corpus; ignored
extracted/                  one ExtractedDoc JSON per source; ignored
index/                      manifest, stats, vector/graph artifacts; ignored
```

Later planned packages are `kng/store/`, `kng/retrieval/`,
`kng/generation/`, and `kng/api/`. Add them only with their work package.
WP0-WP3 produce the portable data plane; WP4-WP5 consume it as the local serving
plane.

## Data and pipeline contracts

The flow is:

```text
raw source -> extract -> normalize -> chunk -> embed -> vector index
                                      \-> graph extraction -> graph store
query -> vector + keyword + graph retrieval -> grounded cited synopsis
```

`Segment` is the authoritative extracted unit. `Chunk` is derived from one or
more segments and flattens provenance for filtering and citations. When changing
models:

- use Pydantic `Field(default_factory=...)` for mutable defaults;
- preserve compatibility with already-written JSON where practical;
- keep locators 1-based for pages/slides and seconds for video spans;
- use project-relative source paths;
- never discard `text_original` in favor of cleaned/translated text; and
- ensure `Segment.citation_label()` and chunk citation metadata still resolve.

Metadata is derived from corpus paths by `kng/pipeline/metadata.py`. Extend it
conservatively: real filenames are inconsistent, and unknown metadata is better
than a confident wrong date, publication, or source type.

The manifest is keyed by project-relative path and SHA-1 content hash. A changed
hash resets stage progress for that file. New stages must use `needs`, `mark`,
and `save`, and must survive interruption without corrupting completed work.

## Extraction behavior

Revision 1 requires every supported file to take a Sarvam path in the paid run:

| Input | Primary paid path | Offline/failure fallback |
|---|---|---|
| JPG/PNG | Sarvam document OCR | Tesseract or no OCR |
| PDF | Sarvam document OCR for the whole file | PyMuPDF page text |
| DOC/DOCX | Local parse, then Sarvam LLM cleanup | Raw local text |
| PPT/PPTX | Local parse, then Sarvam LLM cleanup | Raw local text |
| XLS/XLSX/CSV | Local parse, then Sarvam LLM cleanup | Raw local text |
| Video/audio | ffmpeg + Sarvam ASR in timestamped chunks | faster-whisper or no ASR |

The cleanup prompt must preserve all content and its original language, structure
it as readable Markdown, and explicitly prohibit summarization. Provider errors
must fall back per file and must not terminate the batch. Record the error or
fallback clearly enough to audit later.

The current extension map advertises some legacy formats that do not yet have
matching implementations (`.rtf`, `.ppt`, `.xls`, `.csv`, and some audio/video
extensions). Do not claim full support until dispatch and verification cover
them. Legacy `.doc` requires LibreOffice/`soffice`.

## Progress accounting

Every work package and pipeline stage must report document counts. WP1b defines
the persisted stage schema in `index/stats.json`:

```text
total, processed, skipped, errors, segments, by_type,
sarvam_calls: {ocr, cleanup, asr, translate}, updated
```

Counts must distinguish files from segments and calls. Incremental skips must
not be reported as newly processed. Each WP handover records the actual counts
from verification; do not copy old corpus estimates as new results.

## Development workflow

Use Python 3.11 when possible and the existing `.venv`.

```bash
source .venv/bin/activate
uv pip install -e .
uv pip install -e '.[local]'  # only when local embeddings/ASR are needed
```

Safe current checks that do not make paid calls:

```bash
python -c "import kng, kng.config, kng.models, kng.providers, kng.pipeline.run"
python -m kng.pipeline.run --dry-run
python -m kng.pipeline.run --stage extract --no-ocr --no-asr --limit 1 --force
```

After WP1b is implemented, the standard offline extraction verification becomes:

```bash
python -m kng.pipeline.run --stage extract --local-only --force
python -m kng.stats
```

Do not run `python -m kng.pipeline.run --stage extract` without offline flags;
with a configured `.env`, it can incur paid OCR/ASR and later cleanup calls.

For each change:

1. Inspect the current handover and relevant implementation before editing.
2. Make the smallest work-package-scoped change.
3. Add tests for pure logic and regressions where feasible.
4. Run import/compile checks plus focused offline tests.
5. Exercise interruption, fallback, and incremental behavior when affected.
6. Review `git diff` and avoid touching unrelated user changes.
7. Report exactly what ran, what did not run, and why.

Network/provider integration must be mocked or left for a user-run live smoke
test. Never weaken the paid-call guardrail merely to make a test convenient.
Run bulk ingestion, embedding, and graph-building commands on the cluster.
After WP3, exercise retrieval and UI work locally against copied artifacts.

## Code conventions

- Target Python `>=3.10`; keep type hints and `from __future__ import annotations`.
- Use `pathlib.Path`, UTF-8, project-relative artifact paths, and
  `ensure_ascii=False` for multilingual JSON.
- Keep provider clients lazy/cached so imports and offline discovery do not
  initialize heavy models or call networks.
- A bad source file must not kill a batch. Capture per-file errors and continue.
- Save manifest/stats periodically during long runs, not only at successful exit.
- Prefer deterministic IDs and stable ordering so reruns do not duplicate data.
- Keep extraction, provider access, storage, retrieval, and generation as
  separate layers.
- Avoid broad exception swallowing unless the fallback is intentional and
  observable.
- Do not add infrastructure dependencies when an embedded portable solution
  satisfies the work package.

## Work-package completion

A WP is complete only when its code runs and is verified offline. At the end:

- create or update `docs/handovers/WP<n>-<name>.md`;
- update `docs/WORK_PACKAGES.md`;
- update the status/resume point in `README.md` and `CLAUDE.md`;
- record commands run and real per-stage document/segment/error/call counts;
- document decisions, fallbacks, gaps, and the exact next resume point; and
- leave paid/full-corpus execution as an explicit user action when applicable.

WP3's handover must additionally provide the exact cluster-to-local copy or
packaging procedure, artifact sizes/checksums, required local dependencies, and
a post-copy smoke test. WP4 must treat the copied index as read-only input unless
the user explicitly requests local re-indexing.

Use the existing handover template: what was built, how to run it, decisions and
trade-offs, verification, counts, known gaps, and what the next WP picks up.

## Quality bar for the finished system

Verification should ultimately include:

- a SECI single-meet end-to-end smoke test;
- a cited synopsis whose citations open real files and exact pages/timestamps;
- a TTD Laddu/ghee cross-meet timeline;
- at least one cited OCR newspaper clip and one cited video span;
- multilingual retrieval and response behavior;
- incremental re-ingestion of one changed source; and
- evaluation of retrieval recall, citation correctness, faithfulness, and
  low-confidence refusal.

Treat OCR/ASR output as noisy evidence, political claims as attributed claims
rather than established facts, and citation correctness as a release-blocking
requirement.
