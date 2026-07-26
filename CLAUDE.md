# CLAUDE.md — KNG

Hybrid GraphRAG over the **YS Jagan press-meet archive** (~655 files / 584 MB,
Telugu-dominant + English + Hindi). A user asks a question → grounded synopsis
with **exact source citations**, plus cross-meet / temporal reasoning.

## Non-negotiables

- **Never run paid Sarvam calls during dev.** The user runs paid passes. Verify
  with local-only stages (`chunk`, `embed`, `query` are all free). This is the
  standing guardrail. Note `--local-only` is now *destructive* on this repo —
  see "Verification" below.
- **Sarvam key lives only in git-ignored `.env`.** Never commit it. The
  top-level `/data/` source archive (584 MB) is also ignored. **`index/` and
  `extracted/` ARE committed** so a clone reproduces the whole system — see
  "Cluster → local hand-off". The ignore rule is `/data/`, anchored: an
  unanchored `data/` also matches `extracted/data/` and `index/chunks/data/`,
  which silently excluded 2100 of 4743 artifact files until it was caught.
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
- **Local:** `git clone` is the whole hand-off — `index/` and `extracted/` are
  committed (~204 MB), so WP4 (query engine) and WP5 (chat UI) need no bulk
  processing and **no `SARVAM_API_KEY`**. Querying makes zero API calls.

```bash
git clone <repo> KNG && cd KNG
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[local]'          # bge-m3 for query-side embedding
cp .env.example .env               # key only needed to resume WP3
python -m kng.graph_query stats
```

- `/data/` (584 MB of source files) stays out of git; it is input to stages
  already completed and is not needed to query.
- `scripts/package_index.sh` tars `index/` + `extracted/` with a checksum — the
  alternative if the repo is ever made artifact-free again. Committing binary
  LanceDB files means every re-index adds another full copy to git history, so
  if the repo grows unwieldy, re-ignore both directories and use that script.
- WP6's `kng/pipeline/export.py` remains the formal packaging step.

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

**Current resume point: WP6 (eval, hardening, export).** WP3's paid pass is
**fully complete — 4251/4251 units, entire corpus**, WP4 answers with graph facts,
and WP5 ships the **PressMeets RAG** web app (auth, streamed answers, clickable
citations, history, admin). No paid work remains. State as of 2026-07-26:

| stage | result |
|---|---|
| extract (WP1b) | 634/635 files · **2323 segments / 7.97M chars** · 651 paid calls · 1 known skip |
| chunk (WP2) | 635 files · **4267 chunks** · max 1394/8192 tokens |
| embed (WP2) | **4267 rows** in `index/lancedb` · `bge-m3`, 1024-dim |
| graph — structural (WP3, free) | **698 nodes · 714 edges** · all 33 meets · 634 sources |
| graph — LLM extraction (WP3, **done, full corpus**) | **4251 / 4251 units · 1 unrecoverable failure** (17KB non-JSON reply) · 4234 cached records |
| graph — **final** in `index/graph` | **8120 nodes · 10773 edges · 1157 communities** (22 summarised — the rest are singletons or pairs below phase E's `min_size=3`) |
| query (WP4) | `kng.answer` — hybrid RRF + graph facts + verified citations · cold 13.8 s (bge-m3 load) / **warm 0.22 s** per query |
| app (WP5) | **PressMeets RAG** — `uvicorn kng.api.main:app` · auth with revocable sessions, SSE token streaming, citation → passage viewer, **History page** (search/rename/delete/reopen), **admin console** (roles, password reset, account deletion) · real answer measured at 352 deltas / 17 s with 0 invalid citations · 79 tests + 31 browser checks |

No paid extraction work remains. To re-run after a prompt-version bump or new
source files (re-bills only what changed — the content-hash cache is the record):

```bash
LLM_REASONING_EFFORT=null KNG_LLM_TRACE=1 nohup .venv/bin/python \
  -m kng.pipeline.run --stage graph --concurrency 4 > graph.log 2>&1 &
echo $! > graph.pid        # kill by THIS pid; never `pkill -f`
```

**Use concurrency 4** — more makes latency worse (fact 7). `GRAPH_SOURCE_TYPES`
narrows to a source-type subset if only some new files should be scoped in;
benchmark configs first with `scripts/bench_extract.py`.

Handovers: [WP1b](docs/handovers/WP1b-sarvam-revision.md) ·
[WP2](docs/handovers/WP2-index.md) · [WP3](docs/handovers/WP3-graph.md) ·
[WP4](docs/handovers/WP4-query.md) · [WP5](docs/handovers/WP5-app.md).

Run the app (see [WP5 handover](docs/handovers/WP5-app.md) for the security posture):

```bash
pip install -e '.[api]'
python -m kng.api.users add --email you@example.com --admin
KNG_SESSION_SECRET=$(openssl rand -hex 32) uvicorn kng.api.main:app --port 8000
KNG_FAKE_LLM=1 KNG_SESSION_SECRET=dev uvicorn kng.api.main:app   # free UI work
```

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

⚠️ **`--no-summaries` destroys existing community summaries.** Phase D rewrites
`index/graph/communities.json` wholesale on every graph run, so skipping phase E
leaves all 599 communities with empty `title`/`summary` — 22 paid calls silently
discarded (learned 2026-07-26). The flag is only safe on a graph that has no
summaries yet. Re-running the stage without the flag regenerates them.

**Operational:** never run two extract passes concurrently — they double-bill and
race on `extracted/`. Long CPU stages get killed on the head node at ~2100% CPU;
embed survived at `OMP_NUM_THREADS=12`, but prefer a batch job for WP3.

**Sarvam integration — eight hard-won facts** (full detail in
[WP3 handover](docs/handovers/WP3-graph.md)):

1. **`sarvam-m` is deprecated** — the API 400s on it. This is the real cause of
   WP1b's `cleanup=0` across all 79 office docs; every LLM call had been failing.
   Use `sarvam-105b` (`sarvam-30b` failed the same probe).
2. **`reasoning_effort=null` beats `"low"` — corrected 2026-07-26.** The
   original finding (low: 44 entities / 29 relations / 0 failures vs medium's
   25 / 4 / 1, and `null` under-extracting ~6×) held for 2400-char chunks. It
   does **not** hold at today's ~1500-char units. Benchmarked over the same 13
   real units (`scripts/bench_extract.py`):

   | config | units/min | calls/unit | truncated | entities/unit | relations/unit |
   |---|---:|---:|---:|---:|---:|
   | 105b/low | 3.26 | 1.25 | 3 | 11.4 | 3.9 |
   | 30b/low | 4.14 | 1.75 | 2 | 7.0 | 2.8 |
   | 30b/null | 6.81 | 2.0 | 0 | 11.2 | 6.4 |
   | **105b/null** | **6.02** | **1.25** | **0** | **12.9** | **8.8** |

   `null` is 1.85× faster *and* extracts more, because reasoning tokens no longer
   eat the 4096-token output cap: nothing truncates, so the halve-and-retry
   salvage path stops firing. Run with `LLM_REASONING_EFFORT=null`.
   **`sarvam-30b` is not worth it** — with reasoning it finds 39% fewer entities;
   without, it is no better than 105b/null and needs more calls.
3. **Starter tier caps output at 4096 tokens** — the reason chunks are split and
   truncated JSON is repaired rather than discarded. The API says so outright:
   `max_tokens (8192) exceeds the maximum allowed for sarvam-105b for your
   subscription tier (starter): 4096`. So packing several chunks into one call to
   cut request count is impossible on this tier. With `reasoning_effort=null`
   (fact 2) the cap stops binding in practice.
4. **`tool_choice="required"` makes the model emit nothing**; use `"auto"`.
5. **Keep-alive connections go stale and hang forever** — this killed four runs.
   Every worker parks in `poll_schedule_timeout` on a socket that is
   `ESTABLISHED` locally but dead server-side, while a fresh connection answers
   in 1s. Fixed with `max_keepalive_connections=0` + `Connection: close`.
   Rate limiting made it *worse* by adding idle gaps.
6. **The SDK's timeouts never fire.** Chat goes through direct REST
   (`sarvam.chat_completion`), as OCR already did. Rate limit is **40 req/min
   per account** on Starter; `LLM_RPM` keeps us under it.

7. **Long silences in `graph.log` are the heavy tail, NOT a hang. Do not kill the
   run.** Learned the expensive way on 2026-07-25: two passes were killed after
   11 and 12 minutes of frozen log, and both were working fine — the next
   `[n/total]` line landed on its own. The arithmetic: progress prints every **5
   completions**, and one unit can issue up to **9 HTTP attempts** (`extract_chunk`
   = 1 call, then 2 more on the halves when the reply is empty, each retried by
   `_attempt`). At `LLM_TIMEOUT=90` + `LLM_RETRIES=2` that is ~13 min of one
   worker for one unit, so with N workers all on tail units the log can sit
   still for a quarter of an hour with nothing wrong.
   **Before concluding anything, wait ≥15 min**, then check liveness by port
   churn — `ss -tnp | grep <pid>` sampled twice 20 s apart; changing local ports
   mean requests are cycling. A separate-process probe answering fast proves
   only that the API is up, *not* that the run is stuck.
   Measured rate on the laptop at `--concurrency 3`, `LLM_RPM=10`,
   `LLM_TIMEOUT=60`, `LLM_RETRIES=1`: **~4.3 units/min → ~12 h for 3183 units.**
   The tier's 4096-token output cap is what creates the tail (empty reply →
   halve → re-call). **`LLM_REASONING_EFFORT=null` removes the tail entirely**
   (fact 2): 0 truncations over 1718 units, ~15 units/min, and the long silences
   stopped happening.

8. **Model output is a request, not a guarantee — every consumer must tolerate
   junk.** Two real defects, both from one malformed reply in thousands:
   `_merge_records` subscripted `e["type"]` and an entity without it raised
   `KeyError` inside a worker, which `fut.result()` re-raised on the main thread
   and **killed a 4.6-hour pass** (almost certainly what stopped the original
   cluster run at 1043 units); and `validate` called `.get` on what the model
   sometimes returns as a bare string (`["YS Jagan"]` instead of
   `[{"name": …, "type": …}]`), costing the unit. Both now skip-and-count
   (`bad_entity`, `untyped_entity_string`), the worker loop catches any exception
   as `worker_error` and continues, and `tests/test_graph_cache.py` pins both.

Also `_unwrap` ends in `return str(resp)`, so an unexpected shape becomes fake
"model output"; the graph path uses a strict variant.

**One pass at a time, and kill by recorded pid.** Two overlapping runs double-bill
and race on `index/graph/`; on 2026-07-25 a run believed dead kept going for 4.6 h
alongside its replacement. Write the python pid to `graph.pid` at launch and kill
that — `pkill -f "kng.pipeline.run"` also matches the shell issuing it.

**Triage that actually settles "hung or slow?"** — `py-spy` needs ptrace
privileges and is usually unavailable, and thread `wchan` shows
`poll_schedule_timeout` for both a healthy in-flight request and a dead one.
What decides it: run one timed call from a *separate* process
(`kng.providers.sarvam.chat_completion`, or `graph_extract.extract_chunk` on a
real chunk). Fast answer there + zero `[n/total]` lines in `graph.log` = the run
is hung, kill it. Progress prints every 5 completions, so silence past a few
minutes is real.

Triage for "hung or just slow?":
`for t in $(ls /proc/<pid>/task); do cat /proc/<pid>/task/$t/wchan; echo; done | sort | uniq -c`

**Graph cost gate:** one DSC merit-list PDF is 598 chunks of `<td>` rows (a second
is 152). `graph_extract.select_chunks` trims 4267 chunks to 2950, then splitting
over-long chunks raises it to 4251 units. Skipped chunks stay searchable in
LanceDB.

**`GRAPH_SOURCE_TYPES` is the big cost lever.** 59% of paid units (2528 of 4251)
are `source_doc` — third-party PDFs filed as evidence (court orders, SECI tariff
sheets, DSC merit lists), not anything Jagan said.
`GRAPH_SOURCE_TYPES=press_release,news_clip,video,slide` selects **1718 units**
instead, and the rest can be added later at content-hash granularity with nothing
re-billed. Empty (the default) means all types.

**Config is frozen at import.** `Settings` evaluates every `_env(...)` in its
class body, so `os.environ` changes after `kng.config` is imported have no effect
— `settings.cache_clear()` does not help either. Env vars must precede the
interpreter (`GRAPH_SOURCE_TYPES=… python -m kng.pipeline.run`); tests and
benchmarks must patch the accessor or spawn a subprocess.

**Cache fingerprints are per record, not per file** (`_fp` on each entry). A
file-level fingerprint meant switching model or reasoning effort discarded every
paid record under it. `_fp_acceptable` accepts any real provider/model at the
current `PROMPT_VERSION` and always rejects `FakeLLM`, so the fixture guardrail
survives while 105b and 30b records coexist.

### WP5 app facts

**The `final` SSE event is authoritative; deltas are provisional.** Citations
cannot be verified until the model stops, so `stream_answer` streams raw text and
then emits `final` with `verify_citations` applied. The client must replace what it
streamed — showing the raw stream as finished would present a hallucinated `[9]`
as validated.

**Sources need `chunk_id` and `page` to be openable.** `build_sources` carries
both; without them the viewer falls back to a file's first chunk, so a citation
reading "p.7" silently opens p.1. Caught in WP5 verification.

**`var/` is git-ignored and must stay so** — it holds scrypt password hashes,
per-user chat history, and a query log of real questions.

**`KNG_SESSION_SECRET` has no default**; the app refuses to start without it,
because a predictable signing key lets anyone forge an admin cookie. Auth is
app-level for a small trusted deployment: loopback by default, no TLS, no CSRF
tokens, no self-service password reset — put it behind an HTTPS proxy before
exposing it.

**Sessions are revoked through the user record, not a session table.** Each record
carries `cred_version`; `issue_token` pins it as `cv` and `user_from_token`
compares the two on every request. Without it a password reset changed only what
the owner types — a cookie stolen beforehand kept working until it expired, while
the admin who reset it believed the account was secured. A token with no `cv`
counts as version 1, so adding this signed nobody out. `set_password` bumps it;
deleting or disabling an account is caught by the same re-read.

**Every login path must do one scrypt verification.** The disabled-account branch
used to return before hashing, so it answered ~80 ms faster than a wrong password:
the message said "invalid email or password" while the response time said "this
account exists and is switched off". The login throttle counts per IP **and** per
account — behind a proxy all users share one IP, and one IP spraying one password
across many accounts never trips a per-IP counter.

**Deleting an account purges its history** (`history.purge_user`). Questions must
not outlive the account that asked them. Guards, enforced server-side and mirrored
as disabled buttons: no deleting/disabling yourself, no removing or demoting the
last enabled admin, and a delete must echo the address in `confirm`.

**In CSS, a class that sets `display` beats the `hidden` attribute.** `el.hidden =
true` left "No conversations yet." on screen above a full list, because
`.empty-note { display: flex }` won. `[hidden] { display: none !important }` is in
`styles.css` — do not remove it.

**Screenshot the UI before believing it.** Both of the above were invisible to the
API tests and obvious in a screenshot. `playwright` drives system Chrome with
`executable_path="/usr/bin/google-chrome"` (no browser download); point it at a
throwaway `KNG_VAR_DIR` with `KNG_FAKE_LLM=1` so a run never touches real user
state and costs nothing. Two harness traps: drawers animate for 180 ms, so wait
before capturing or you photograph a mid-slide panel; and a `page.once("dialog")`
handler that never fires stays registered and hijacks the next dialog.

**`auth.var_dir()` and `auth._secret()` read the environment at call time**, not
through `Settings`, precisely because settings freeze at import — that is what lets
tests redirect state and supply a secret after importing the app.

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
kng/config.py models.py stats.py query.py(WP2 retrieval smoke test) answer.py(WP4 CLI)
kng/providers/    sarvam.py llm.py embeddings.py ocr.py asr.py translate.py
kng/pipeline/     manifest.py metadata.py normalize.py chunk.py embed.py graph_build.py run.py export.py
kng/pipeline/extract/  documents.py media.py
kng/store/        vector.py(LanceDB, WP2)  graph.py(WP3)
kng/retrieval/    hybrid.py(vector+BM25+RRF) graph_context.py   # WP4
kng/generation/   synthesize.py(prompt + citation verification, stream_answer)
kng/api/          main.py auth.py users.py meta.py sources.py history.py  # WP5
kng/api/static/   index/login/history/admin .html · app.js history.js admin.js styles.css
scripts/          bench_extract.py package_index.sh
tests/            test_wp4.py test_graph_cache.py test_api.py   # 79 tests
var/              users.json history/ queries.jsonl   # WP5 state, git-ignored
config/ontology.yaml   docs/   extracted/
scripts/ package_index.sh               # tar index/+extracted/ with checksum
index/   manifest.json stats.json chunks/ lancedb/ graph/   # committed to git
extracted/                               # committed; citations resolve against it
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
python -m unittest discover -s tests         # 79 tests, no network or key needed
python -m kng.answer "TTD laddu" --retrieval-only    # WP4 evidence, no LLM call
KNG_FAKE_LLM=1 python -m kng.answer "TTD laddu"      # WP4 end-to-end, offline
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
