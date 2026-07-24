# CLAUDE.md — KNG

Hybrid GraphRAG over the **YS Jagan press-meet archive** (~655 files / 584 MB,
Telugu-dominant + English + Hindi). A user asks a question → grounded synopsis
with **exact source citations**, plus cross-meet / temporal reasoning.

## Non-negotiables

- **Never run paid Sarvam calls during dev.** Build + verify offline with
  `--local-only`; the user runs the full paid pass. This is the standing guardrail.
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

## Provider stack (locked)

Sarvam primary: LLM `sarvam-m`, ASR `saarika:v2.5`, OCR/Doc-Intelligence
`sarvam-vision`, translation `mayura:v1`. **Sarvam has no embeddings API** →
embeddings run locally on `intfloat/multilingual-e5-base` (needs `.[local]`).

## Work-package workflow

Build in numbered, independently-resumable **work packages (WPs)**. A WP is
"done" only when its code runs and is verified. End every WP with a **handover
doc** in `docs/handovers/` and update `README.md` + `docs/WORK_PACKAGES.md`.
Always record per-stage **document counts** in the handover.

See [docs/WORK_PACKAGES.md](docs/WORK_PACKAGES.md) for the WP tracker and
[docs/handovers/](docs/handovers/) for handovers. Approved plan:
`~/.claude/plans/act-as-a-software-snuggly-pond.md` (Revision 1 = Sarvam-first
universal extraction + progress tracking).

**Current resume point:** WP1b — see
[docs/handovers/WP1b-sarvam-revision.md](docs/handovers/WP1b-sarvam-revision.md).

## Layout

```
kng/config.py models.py stats.py(WP1b)
kng/providers/    sarvam.py llm.py embeddings.py ocr.py asr.py translate.py
kng/pipeline/     manifest.py metadata.py normalize.py chunk.py embed.py graph_build.py run.py export.py
kng/pipeline/extract/  documents.py media.py
kng/store/ retrieval/ generation/ api/    # WP2–WP5
config/ontology.yaml   docs/   index/   extracted/
```

## Offline verification (no paid calls)

```bash
source .venv/bin/activate
python -c "import kng, kng.config, kng.models, kng.providers, kng.pipeline.run"  # imports clean
python -m kng.pipeline.run --stage extract --local-only --force   # offline extract pass
python -m kng.stats                                               # per-stage doc counts (WP1b+)
```
