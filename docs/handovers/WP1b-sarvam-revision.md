# Handover — WP1b: Sarvam-first universal extraction + progress tracking

**Status:** ✅ **Done — paid Sarvam pass executed on the cluster 2026-07-25.**
**634/635** files extracted + normalised into **2323 segments / 7.97M chars**.
One file is knowingly skipped (legacy non-Unicode Telugu font).
Ready for WP2 — but read defects 6–9 first: the artifacts needed repairing
after the pass, and `extracted/` today is the *repaired* version.
Prompted by two user requirements after WP1:

1. **Every document must go through the Sarvam API — even text extraction**
   (not just images/video).
2. **Always track how many documents were processed per stage / work package.**

Design decisions confirmed with the user:
- Office docs (docx/pptx/xlsx): **local text → Sarvam LLM cleanup** (not
  LibreOffice→PDF). PDFs & images: **Sarvam Document Intelligence OCR**.
- **Sarvam is primary; local parse is fallback** (Sarvam fails / no key / `--local-only`).

See `Revision 1` in the approved plan: `~/.claude/plans/act-as-a-software-snuggly-pond.md`.

## Deployment note (Revision 2, plan)

The user runs all data-processing WPs (this one, WP2, WP3) **on a cluster**;
after WP3 finishes, `index/` (+`extracted/`) is copied to their **local**
machine, where WP4 (query engine) and WP5 (chat web UI) are built and run.
So WP1b's Sarvam extraction pass and its downstream WP2/WP3 stay on the
cluster — no change to this WP's plan, just confirms where it executes.

## What was built (all done)

1. **`kng/stats.py`** (new) — per-stage counters persisted to `index/stats.json`:
   `set_stage(stage, counts)`, `render()`, and `python -m kng.stats`. Counts
   `{total, processed, skipped, errors, segments, by_type, sarvam_calls:{ocr,cleanup,asr,translate}}`.
2. **`kng/models.py`** — `ExtractedDoc.sarvam_calls: dict[str,int]` — each extractor
   tallies the paid calls it triggered into this shared counter.
3. **`kng/providers/llm.py`** — `clean_document(text, lang_hint="")` on both
   `SarvamLLM` and `AnthropicLLM`. Strict prompt: reformat raw office-doc text to
   Markdown, **preserve ALL content, do not summarise/translate, keep original
   language**. Output-token budget scales with input length so long releases aren't
   truncated.
4. **`kng/pipeline/extract/documents.py`**
   - `extract_pdf`: `sarvam and use_ocr` → Sarvam OCR the **whole** file
     (`_pdf_via_sarvam`); on failure/empty falls back to local PyMuPDF text
     (`_pdf_via_pymupdf`). `sarvam=False` → PyMuPDF only, no OCR call.
   - `extract_doc/pptx/xlsx`: local parse, then `_maybe_clean()` routes the text
     through `clean_document()` when `sarvam and use_cleanup`. Guard: if the cleaned
     text is <50 % of the input length (possible truncation/summarisation) it keeps
     the raw local text. Falls back to raw on any error / when `sarvam=False`.
   - Every kind of call is tallied via `_bump(calls, "ocr"|"cleanup")`.
5. **`kng/pipeline/extract/media.py`** — `extract_image`/`extract_video` now take
   `sarvam` + `calls`; OCR/ASR require **both** their feature flag *and* `sarvam`
   (so `--local-only` makes zero calls and simply yields no segments), and tally
   `ocr`/`asr`.
6. **`kng/pipeline/normalize.py`** — tallies `translate` into `doc.sarvam_calls`
   when a segment is actually translated.
7. **`kng/pipeline/extract/__init__.py`** — `extract_file(..., sarvam=True,
   use_cleanup=True)`; owns the shared `calls` dict (= `doc.sarvam_calls`) and
   threads it to every extractor.
8. **`kng/pipeline/run.py`** — new flags **`--local-only`** (sarvam=False, fully
   offline) and **`--no-cleanup`**; `--translate` is auto-disabled under
   `--local-only`. `run_extract` aggregates `by_type` + `sarvam_calls`, calls
   `stats.set_stage("extract", …)`, and prints a rollup:
   `extract [local-only]: 645/645 done · 1655 seg · 2 err · 0 sarvam calls`.

## Verification (offline, no paid calls)

```bash
source .venv/bin/activate
python -c "import kng, kng.stats, kng.providers.llm, kng.pipeline.run"   # imports clean
python -m kng.pipeline.run --stage extract --local-only --force          # 29 s, 0 calls
python -m kng.stats
```

Result of the full offline pass over all **645** files:

```
extract  645 files → 645 processed · 0 skipped · 2 errors · 1655 segments
         by_type: news_clip=353 press_release=63 slide=13 source_doc=188 table=3 video=25
         sarvam:  ocr=0 cleanup=0 asr=0 translate=0   ← guardrail: fully offline
```

- **2 errors**: two legacy `.doc`/`.RTF.doc` files that need LibreOffice
  (`soffice`) on PATH — unavailable in this dev shell; they'll convert on the
  cluster (or stay as known skips). All other 643 files extracted cleanly.
- In `--local-only`, `news_clip` (353) and `video` (25) yield 0 segments because
  OCR/ASR are Sarvam-only; the 1655 segments come from PDFs (PyMuPDF text),
  press releases, slides and tables. The paid pass fills in the rest.

## Running the paid pass (user, on the cluster — with `SARVAM_API_KEY`)

**1. Validate on one news clip** (confirm the DI OCR schema before spending):

```bash
python -m kng.pipeline.run --stage extract --force --only "women/1.jpeg"
python -m kng.stats            # expect sarvam_calls: ocr=1
```

**2. Full pass — resumable.** The offline pass left stale `done` marks in
`index/manifest.json`, so clear it, then run *without* `--force` so the run is
resumable (the manifest checkpoints every 25 files; a killed run resumes on
plain re-run instead of re-paying from zero):

```bash
rm -f index/manifest.json
nohup python -m kng.pipeline.run --stage extract > extract.log 2>&1 &   # long: ~540 OCR jobs
tail -f extract.log
python -m kng.stats                                                     # sarvam_calls populated
```

This whole-file-OCRs every PDF/image, LLM-cleans every office doc, and ASRs every
video — hundreds of sequential API jobs, so it takes a while (run under
`nohup`/`tmux`). Add `--translate` to also store English. Watch `sarvam_calls` in
the rollup to confirm every document went through the API (the WP1b requirement).
Optional: `--no-ocr/--no-asr/--no-cleanup` to skip a modality.

## Paid pass — results (2026-07-25, cluster)

Final state, after the retry pass, the artifact repair, and the long-PDF re-OCR:

```
extract  635 files → 635 processed · 0 skipped · 1 errors · 2323 segments
         by_type: news_clip=343 press_release=63 slide=13 source_doc=188 table=3 video=25
         sarvam:  ocr=626 cleanup=0 asr=25 translate=0  (total 651)
```

**7,967,484 characters** of real text. Segments by language:
`en=1341 te=872 mixed=108 hi=2`.

Verified across the whole corpus: **0 base64 payloads**, **0 PDFs without OCR**,
**0 PDFs with collapsed page numbering**, 0 `unknown`-language segments.
Every PDF, image and video in the archive has now been through Sarvam; only the
office-doc cleanup path remains unmet (deliberately — see below).

> The first rollup read *1907 segments / 57.6M chars*. That character count was
> 89% base64 image data (see defect 7); the segment count was low because
> multi-page PDFs were collapsing into one segment (defect 8). Both are fixed —
> the corpus did not shrink, it was measured wrong.

⚠️ **`cleanup=0` is a silent failure, not a no-op.** `_bump(calls, "cleanup")`
fires only *after* `clean_document()` returns, so zero cleanup calls across the
79 office docs (63 press releases + 13 slides + 3 tables) means **every** call
raised and was swallowed by the bare `except` in `_maybe_clean`. Those docs
carry raw local-parse text, so **no content was lost**, but the WP1b
"every document goes through Sarvam" requirement is **unmet for office docs**.
`get_llm()` itself resolves fine (`SarvamLLM`, `clean_document` present), so the
failure is inside the chat call — diagnose with one press release before
re-running the batch. `_maybe_clean` now tallies `cleanup_failed` /
`cleanup_lossy`, and `kng.stats` prints them on an `issues:` line, so this can
never fail silently again.

### What went wrong in the run, and the fixes

**Two extract passes ran concurrently** (a `nohup` background job that ended
11:35 and a foreground `--force` run that ended 11:42). Each made its own ~506
paid calls, so the pass was billed roughly twice, and the two processes wrote
`extracted/` simultaneously. The transient `RemoteProtocolError`s below are
consistent with both hammering the API at once. **Run one pass at a time.**

1. **`--force` wiped the manifest** (`run.py`). `man.needs()` is the only thing
   that registers a file, and the force path skipped it, so `man.mark()` no-opped
   and the final `man.save()` overwrote a good 645-entry manifest with `{}` —
   the incremental tracking for the whole paid pass. Fixed: `needs()` is now
   always called and `force` only overrides the skip decision.
2. **Manifest rebuilt without re-extracting**: new `--rebuild-manifest` flag
   re-derives per-file hash + stage status from the `extracted/` docs already on
   disk, so a lost manifest never costs a second paid pass.
3. **`~BROMIUM` dirs excluded from discovery** (`SKIP_DIR_PREFIXES`). These
   Bromium micro-VM stubs are 216–426-**byte** files masquerading as `.jpg`s;
   all 10 failed OCR with *"Invalid or corrupted image file"*. This is why the
   file count is now **635**, not 645.
4. **Silent PDF data loss fixed** (`documents.py`). `_pdf_via_sarvam` swallowed
   the OCR exception and returned `[]`; for a *scanned* PDF the PyMuPDF fallback
   then also found nothing, so the doc was saved with **0 segments and marked
   `done`** — permanently missing, never retried. Two real documents hit this.
   `extract_pdf` now raises when OCR failed *and* there is no text layer.
5. **`ExtractedDoc.file_hash` is now populated** (was always `""`), so
   `extracted/` is self-describing for future rebuilds.
6. **Sarvam DI rejects PDFs over 10 pages** — `400 "PDF has 14 pages, maximum
   allowed is 10."` This surfaced only once defect 4 stopped swallowing the
   error. **All 47 PDFs longer than 10 pages silently bypassed OCR** and fell
   back to PyMuPDF. There is no API option to raise the cap (`create_job` takes
   only `language` + `output_format`), so `SarvamOCR` now splits long PDFs into
   ≤`MAX_PDF_PAGES` slices, OCRs each as its own job, and renumbers pages back
   to the original document so citations still resolve. `last_call_jobs` reports
   the real number of billed jobs per file.
7. **89% of the corpus was base64 image data** (`ocr.py`). DI embeds every figure
   it detects as a `![Image](data:image/jpeg;base64,…)` URI in its Markdown, and
   these were stored as segment text: **52,050,250 of 58,335,613 chars**, across
   306 docs (worst offender 4.6M chars, 96.7% base64). WP2 would have chunked and
   embedded it. `strip_data_uris()` now removes them in `_collect_pages`.
8. **Page provenance was collapsing** (`ocr.py`). DI separates pages with a `---`
   horizontal rule, not the form feed `_paginate` looked for, so **all 111
   multi-page OCR'd PDFs became a single page-1 segment** — citations could not
   resolve to a page, violating the provenance-first rule. `split_pages()` now
   splits on both. Verified: the `---` count equals *pages − 1* in 136/141 OCR'd
   PDFs.
9. **`--repair-extracted`** (new) applies 7 + 8 to artifacts **already on disk**,
   so neither defect cost a re-run. Result: 369/631 docs repaired, 52,052,757
   chars of base64 removed, 111 PDFs re-paginated, segments 1910 → 2251,
   `extracted/` 64M → 15M. It re-runs `normalize_doc` (local only) because the
   base64 was ASCII and had been skewing language detection to English —
   Telugu segments went 397 → 676. Idempotent; a second run changes nothing.
   Verified against a backup of all 645 docs: **zero legitimate text lost**.

### Outstanding

Three passes ran after the repair: a 7-file retry (3 paid calls), the 49-file
long-PDF re-OCR (142 paid calls), and a 2-file legacy-`.doc` pass (**0 paid
calls**). **1 file remains skipped:**

| File | Cause | Status |
|---|---|---|
| `DSC GO 31-05-26.RTF.doc` | RTF whose text is set in **Shree-JagatiText**, a legacy Shree-Lipi font that maps Telugu glyphs onto cp1252 bytes | Extractable, but the bytes are meaningless as text. Converting needs a glyph map with Indic reordering and would yield plausible-but-wrong Telugu — worse than nothing for citations. **Deliberately skipped.** |

### Legacy `.doc` handling (no LibreOffice on the cluster)

`soffice` is unavailable, and `module avail libreoffice` finds nothing. The two
files turned out to be different formats — the extension lies — so
`_legacy_doc_text` now dispatches on **magic bytes** and has a pure-Python path
for each, using LibreOffice only when present:

- `{\rtf` → `striprtf`. (`*.RTF.doc` is RTF mislabelled as `.doc`.)
- `\xd0\xcf\x11\xe0` (OLE2) → `olefile`, reading the `WordDocument` stream
  bounded by the FIB's `fcMin`/`fcMac`. Word's in-band cell/row/page marks become
  newlines rather than being dropped, so words either side stay separated.
  This recovered **`TTD Complaint on ARDairy.doc` — 20,414 chars** of the TTD
  adulterated-ghee complaint, directly relevant to the Tirupati laddu meets.

New deps: `striprtf`, `olefile` (both pure-Python, added to `pyproject.toml`).

**`_is_legacy_font_mojibake()` guard**: text with no Unicode Telugu but >15%
cp1252 high bytes is rejected with an explicit error instead of being indexed.
A corpus-wide scan found **0 other affected segments**, so this defect is
contained to the one file — but the guard stops any future drop-in from silently
poisoning the embeddings.

**Done — all 47 long PDFs re-OCR'd** (`extract2.log`, 2026-07-25). The user chose
the full set over the 36-job targeted subset, so every PDF in the archive now
satisfies the Sarvam-first requirement.

```
extract [sarvam]: 49/635 done · 1198 seg · 2 err · 142 sarvam calls
```

Exactly the 142 predicted jobs. Outcome:

- **+1,664,214 chars of real text recovered** (6.28M → 7.95M) and +71 segments,
  from the 82 scanned pages that had no text layer.
- The two 14-page scans that defect 4 had silently dropped came back with
  **14 segments each** — full page-level provenance.
- Language detection improved again as scanned Telugu pages became readable:
  `te` 676 → **872**, `mixed` 61 → **108**, `en` 1505 → **1340**.
- Sarvam Markdown also proved structurally better than the PyMuPDF text it
  replaced (real `##` headings), which should improve WP2 chunk boundaries.

The 2 legacy `.doc` files were retried in the same pass and failed locally on the
missing `soffice` at no API cost.

**Decided — do not chase `cleanup=0`.** Office docs already carry complete raw
text from the local parse; cleanup only reformats to Markdown. The WP1b
"every document through Sarvam" requirement stays knowingly unmet for those 79
docs. The failure is categorical, not size-related (the median office-doc input
is 410 chars and those failed too), so it is auth / model access / endpoint —
diagnosable later with a single minimal chat call if it ever matters.
`cleanup_failed` is now tallied and printed by `kng.stats`, so it stays visible.

## Guardrails
- Do **not** auto-run paid Sarvam calls during dev — user runs the full pass.
  Verify with `--local-only`.
- **Never run two extract passes at once** — they double-bill and race on
  `extracted/`.
- `--local-only --force` rewrites `extracted/` with offline-only output; do not
  run it after a paid pass or it will overwrite real OCR/ASR text.

## OCR schema fix (first paid run)
The first live run 400'd on every news clip: the old `providers/ocr.py` posted a
hand-rolled `doc-digitization/job/v1` body with the wrong schema. The installed
`sarvamai` SDK now wraps this API under `client().document_intelligence`, so
`SarvamOCR` was rewritten to use the SDK's job workflow — `create_job(language=…,
output_format="md")` → `upload_file` → `start` → `wait_until_complete` →
`get_download_links` → download + paginate. Language is a single BCP-47 hint
(first of the requested langs; `te` → `te-IN`; archive is Telugu-dominant).
PDFs already fell back to PyMuPDF on the 400 (so they got text but bypassed
Sarvam); with the fix they route through DI OCR too. **Validate on one clip
before the full pass** (see below).

## Then WP2 (chunk → embed → LanceDB)
`kng/pipeline/chunk.py` (≈500–800 tok, overlap, keep provenance) →
`kng/pipeline/embed.py` (local `multilingual-e5-base`; needs `uv pip install -e
'.[local]'`) → `kng/store/vector.py` (LanceDB under `index/`) → `kng/query.py`
(retrieval smoke). Then plain RAG works end-to-end.
