# WP3 — Knowledge graph build

**Status:** code complete and verified. The paid extraction pass is **running on
the cluster and incomplete** — resumable, re-bills nothing. Everything else
(structural graph, resolution, communities, query CLI) is done and free to run.

---

## Quick resume

`index/` and `extracted/` are committed, so `git clone` + `pip install -e
'.[local]'` gives a working system with no API key. See "Cluster → local
hand-off" below. To continue the paid extraction:

```bash
cd ~/KNG && source .venv/bin/activate

# is the extraction still going?
pgrep -af "kng.pipeline.run --stage graph"
tail -5 graph.log

# resume it (identical command; already-extracted chunks are never re-billed)
# Concurrency 4 — NOT 12. 12 hangs every worker with no timeout and no error;
# see "Concurrency ceiling" below.
nohup .venv/bin/python -m kng.pipeline.run --stage graph --concurrency 4 > graph.log 2>&1 &

# what would it still cost? (zero calls)
python -m kng.pipeline.run --stage graph --plan-only

# free rebuild of the graph from whatever is already extracted
python -m kng.pipeline.run --stage graph --structural-only
python -m kng.graph_query stats
```

---

## What was built

| file | role |
|---|---|
| `kng/graph/ontology.py` | first reader of `config/ontology.yaml`. Builds the extraction schema **and** validates what comes back, so prompt and validator cannot drift. |
| `kng/models.py` (additive) | `Entity`, `Mention`, `Relation`, `Community`. No existing field changed. |
| `kng/store/graph.py` | NetworkX `MultiDiGraph` as node-link JSON under `index/graph/`, plus neighbourhood / path / timeline helpers for WP4, plus an optional Neo4j mirror. |
| `kng/pipeline/graph_build.py` | the stage: phase A (structural) + D (Louvain communities). |
| `kng/pipeline/graph_extract.py` | phases B (LLM extraction), C (resolution), E (summaries) — everything paid. |
| `kng/graph_query.py` | free CLI: `stats`, `entities`, `neighbors`, `path`, `timeline`, `communities`. |
| `kng/providers/llm.py` | `complete_json`, retry/backoff, truncated-JSON repair, call accounting, `FakeLLM`. |
| `kng/providers/sarvam.py` | direct REST chat client, rate limiter, keep-alive fix. |

Modified: `config.py` (graph knobs), `stats.py` (graph call kinds + size line),
`pipeline/run.py` (flags, dispatch fix), `.env`, `.env.example`.

---

## The Sarvam integration — read this before touching extraction

Six defects, found in order. Each one silently produced zero or degraded output.

**1. `sarvam-m` is deprecated.** HTTP 400: *"Model 'sarvam-m' has been
deprecated. Please use one of the available models instead: sarvam-30b,
sarvam-105b."* **This is also the real cause of WP1b's `cleanup=0` across all 79
office docs** — every LLM call in this repo had been failing. Default is now
`sarvam-105b`. `sarvam-30b` failed the same extraction probe.

**2. `reasoning_effort` must be `"low"`.** These are reasoning models. The API
default is `"medium"`, which spends 2500–4000 tokens thinking. Measured over the
same passages:

| effort | entities | relations | failures |
|---|---|---|---|
| **low** | **44** | **29** | **0** |
| medium (API default) | 25 | 4 | 1 |
| `null` (disabled) | 4 | — | skims badly |

Less thinking leaves more of the output cap for the answer, so `low` wins on
quality *and* reliability. `null` is valid (the docs allow it to disable
reasoning) but under-extracts by ~6×.

**3. The subscription tier caps output at 4096 tokens.** *"max_tokens (6000)
exceeds the maximum allowed for sarvam-105b for your subscription tier
(starter): 4096."* With `medium` reasoning this left almost no room and ~50% of
calls returned nothing. Mitigations: `reasoning_effort=low`,
`repair_truncated_json`, and `GRAPH_MAX_CHUNK_CHARS`.

**4. `tool_choice="required"` makes the model emit nothing** — it reasons to the
cap and returns an empty message. Use `"auto"`; it then reliably writes the same
JSON, as a tool call or as prose the parser handles. `response_format`
(`json_object` / `json_schema`) is documented but returned empty output in
testing — not used.

**5. Keep-alive connections go stale and hang forever.** *This killed four runs.*
The server drops idle pooled connections without the close reaching us, so a
socket stays `ESTABLISHED` locally while dead at the far end; the next request on
it is never answered and the worker blocks until the read timeout. Symptom: every
worker in `poll_schedule_timeout`, process at 0.3% CPU, while a fresh connection
from another process answers in **1 second**. Rate limiting made it *worse* by
adding the idle gaps that let connections go stale.
**Fix: `max_keepalive_connections=0` + `Connection: close`.** A new connection
per request costs ~100 ms against calls of 15–40 s.

**6. The SDK's timeouts do not fire.** Neither `request_options=
{"timeout_in_seconds": …}` nor a client-level `timeout=` prevented the hang.
Chat now goes through **direct REST** (`sarvam.chat_completion`), exactly as OCR
already did, where an explicit `httpx.Timeout` does fire.

**Rate limits** (documented): sarvam-30b/105b = **40 req/min on Starter**, 60 on
Pro, 120 on Business, counted **per account, not per key**. `_RateLimiter` keeps
us under it by design; `LLM_RPM` defaults to 35.

**7. A frozen `graph.log` is the heavy tail, not a hang — do not kill the run.**
This cost two needless restarts on 2026-07-25 while resuming the pass on a
laptop. A run at `--concurrency 12` was killed after 11 minutes with zero
completions, and its replacement at `--concurrency 4` after 12 minutes of
silence following an initial 25 units. Neither was stuck: the replacement run,
left alone, printed `[30/3183]`, `[35/3183]`, `[40/3183]` on schedule.

The arithmetic that explains the silence:

- progress prints **every 5 completions** (`commit_every=5`), and
- one unit can issue **up to 9 HTTP attempts** — `extract_chunk` makes one call,
  then two more on the halves when the reply comes back empty, and `_attempt`
  retries each — so at `LLM_TIMEOUT=90` + `LLM_RETRIES=2` a single tail unit
  occupies one worker for **~13 minutes**.

With every worker on a tail unit, a quarter hour of frozen log is normal.

How to tell alive from stuck, in order:

1. **Wait ≥15 minutes.** Anything shorter is not evidence.
2. **Port churn** — `ss -tnp | grep <pid>`, sampled twice ~20 s apart. Changing
   local ports mean requests are cycling; that is a live run.
3. A separate-process probe (below) proves the **API** is up. It does **not**
   prove the run is stuck — during both "stalls" the probe answered in 1.5 s and
   ran 5/5 real extractions in 4.6–28.9 s while the pass was in fact fine.

Also checked and ruled out as explanations: buffering (`/proc/<pid>/fdinfo/1`
`pos` equalled file size, so nothing was withheld) and the request-rate cap
(`LLM_RPM=30` was never approached).

Measured throughput on the laptop at `--concurrency 3`, `LLM_RPM=10`,
`LLM_TIMEOUT=60`, `LLM_RETRIES=1`: **~4.3 units/min → ~12 h for 3183 units.**
The tail exists because the tier's 4096-token output cap produces empty replies
that force the halve-and-recall path, so the tier upgrade flagged below is the
real fix for wall-clock too.

Diagnostic that settles "hung or just slow?":

```bash
# 1. Has anything completed? Progress prints every 5 completions.
grep -ac '^  \[' graph.log

# 2. wchan is suggestive but NOT decisive — a healthy in-flight request and a
#    dead socket both sit in poll_schedule_timeout.
for t in $(ls /proc/<pid>/task); do cat /proc/<pid>/task/$t/wchan; echo; done | sort | uniq -c

# 3. Decisive: one timed call from a separate process. Fast here + zero progress
#    lines there = hung; kill and restart at lower concurrency (nothing re-bills).
python -c "import time;from kng.config import settings;from kng.providers.sarvam import chat_completion;s=settings();t=time.time();chat_completion({'model':s.sarvam_chat_model,'messages':[{'role':'user','content':'ok'}],'max_tokens':200,'reasoning_effort':'low'});print(f'{time.time()-t:.1f}s')"
```

`py-spy dump` would be ideal but needs ptrace privileges that are usually
unavailable — do not count on it.

---

## Defects fixed in existing code

1. **`run.py`'s blanket `except ImportError`** reported a genuine missing
   dependency inside `graph_build.py` as "not implemented yet — skipping". Now
   narrowed to the stage module's own absence.
2. **`_unwrap` ends in `return str(resp)`**, turning an unexpected shape into
   something that looks like model output. Fine for cleanup, not for extraction
   where it becomes entities. `complete_json` uses a strict variant.
3. **The paid phase was gated on the manifest, which desyncs from the cache.**
   The first real gate run reported `0 new calls · 0 failed` and exited cleanly
   having extracted nothing: offline `KNG_FAKE_LLM` verification had marked all
   635 files `graph: done`, and clearing the fixture cache did not clear those
   marks. Work is now decided by the **extraction cache** — the only honest
   record of what has been paid for. Same failure mode as the WP1b/WP2 defects:
   not a crash, a confident report of work that never happened.
4. **Concurrency was structurally capped at ~1.** Threads were pooled *inside*
   each file while files ran sequentially; the median file holds one chunk, so
   6→16 workers moved throughput from ~3/min to ~4/min. `_extract_all` now runs
   one flat queue over every outstanding chunk.

---

## Key design decisions

**Free-first.** Everything derivable from metadata is built with zero paid calls;
LLM enrichment layers on top. `--structural-only` yields a real 33-meet graph
(698 nodes / 714 edges / 21 communities) with no API key, so the stage was
testable before any spend.

**Cache keyed by `content_hash`, fingerprinted by provider/model/prompt version.**
A killed run resumes at chunk granularity; deterministic re-chunking does not
invalidate paid work; duplicate files collapse onto one call. The fingerprint
exists because `FakeLLM` fixtures are otherwise indistinguishable from paid
results — verified that a Sarvam-fingerprinted run reuses 0 records from a fixture
cache. **Bump `PROMPT_VERSION` whenever the prompt or model changes.**

**Chunk selection is a cost gate.** One DSC merit-list PDF is 598 chunks of
`<td>` rows (a second is 152). Blind, that is ~750 calls returning junk `Person`
nodes. Min-chars + markup-density + per-file cap trim 4267 chunks to 2950;
splitting over-long chunks then raises it to 4251 units, which buys coverage
(without splitting, 663 chunks contribute nothing). Skipped chunks stay
searchable in LanceDB and keep their `Source` nodes.

**Louvain, not Leiden** — a documented deviation. `leidenalg` needs
`python-igraph`, a compiled extension that would also have to install on the
laptop; `networkx.community.louvain_communities` ships in an existing dependency
and is seeded for reproducibility.

**Node-link JSON, not pickle or GraphML** — portable across versions, safe to
load, greppable, holds list attributes, readable Telugu on disk.

**Mentions are not edges.** One aggregated `MENTIONS` edge per (PressMeet,
Entity) with weight and capped evidence. Every assertion stays citable; the
artifact stays small.

---

## Document counts

| stage | result |
|---|---|
| extract (WP1b) | 634/635 files · 2323 segments |
| chunk (WP2) | 635 files · 4267 chunks |
| embed (WP2) | 4267 rows · bge-m3 1024-dim |
| graph — structural (free) | 698 nodes · 714 edges · 21 communities |
| graph — selection, all types | 4251 extraction units (2950 chunks + 1301 split pieces) |
| graph — phase 1, **speech-first** (2026-07-26 09:17–10:30) | **1718 / 1718 units, 0 failures** — 4792 nodes / 6402 edges / 599 communities |
| graph — phase 2, **remaining `source_doc`** (2026-07-26 10:46–12:03) | **1934 new calls, 1 retry, 1 unrecoverable failure** (17KB non-JSON reply), 2 duplicates reused |
| **graph — extraction, FULL CORPUS (DONE 2026-07-26)** | **4251 / 4251 units · 4234 cached records · 1 known failure** |
| graph — resolution | 36,620 mentions → 17,020 canonical names · 13,229 mentions · 15,152 relation assertions |
| **graph — final** | **8120 nodes · 10773 edges · 1157 communities** · 22 summarised |

Full-corpus coverage: all **33 press meets**, 634 sources, 30 dates. Entity types:
Organization 2049, Claim 1775, Scheme 1053, Person 1350, Place 634, Issue 497,
Party 64. Relations: MENTIONS 7021, RELATED_TO_ISSUE 882, CITES_SOURCE 633,
MAKES_CLAIM 555, ABOUT_ISSUE 551, LOCATED_IN 415, ANNOUNCED_SCHEME 291,
ACCUSES 262, MEMBER_OF 68, PRECEDES 29, DEFENDS 14.

The one unrecoverable unit is not worth chasing further — `--retry-split` was
already tried and the reply still did not parse; it is 1 of 4251, and the file's
other units still cover its meet.

Community summaries stayed at **22, not more**, after phase 2 — correctly: the
+558 new communities from the evidence-PDF pass are all below phase E's
`min_size=3`, so no new paid summary calls were owed. Largest clusters —
*Power sector policy and regulation* (377 entities), *Liquor Scam and Redbook in
Andhra Pradesh* (324), *Emergency 2.0 policy and controversy* (324).

### The two levers that took phase 1 from ~17 h to 73 min

Measured 2026-07-26 on a laptop, after the pass had been crawling at 3.06
units/min with a 43 % truncation-salvage rate. Phase 2 (the remaining
`source_doc` units, run with the same `LLM_REASONING_EFFORT=null` config) went
even faster — **~26 units/min**, since evidence-PDF units average shorter than
speech units.

| lever | effect |
|---|---|
| `GRAPH_SOURCE_TYPES` speech-first scope (phase 1 only) | calls to bill 3067 → **1131** |
| `LLM_REASONING_EFFORT=null` | 3.26 → **15–26 units/min**, salvage 43 % → **0 %**, held for all 4251 units |

`scripts/bench_extract.py` produced the evidence (13 real units per config,
subprocess each because settings freeze at import):

| config | units/min | calls/unit | truncated | entities/unit | relations/unit |
|---|---:|---:|---:|---:|---:|
| 105b/low | 3.26 | 1.25 | 3 | 11.4 | 3.9 |
| 30b/low | 4.14 | 1.75 | 2 | 7.0 | 2.8 |
| 30b/null | 6.81 | 2.0 | 0 | 11.2 | 6.4 |
| **105b/null** | **6.02** | **1.25** | **0** | **12.9** | **8.8** |

Turning reasoning **off** is both faster and better here, because reasoning
tokens were consuming the tier's 4096-token output cap: nothing truncates, so the
halve-and-retry path stops firing. This **reverses** the earlier
`reasoning_effort="low"` finding, which was measured on 2400-char chunks and does
not hold at the ~1500-char units this corpus actually produces. `sarvam-30b` is
not worth it — 39 % fewer entities with reasoning, no better without.

A benchmark caveat worth remembering: **entity overlap is not a usable quality
metric here.** Re-extracting passages with the *same* model and effort that
produced the cached record scores only 0.237 Jaccard, so the metric measures the
model's nondeterminism (`seed` is documented Beta and evidently not honoured), not
config quality. Compare entities and relations *per unit* instead.

Verified quality, with exact citations:

```
Y. S. Jagan Mohan Reddy --ACCUSES--> Eenadu
  "ఈనాడు, ఆంధ్రజ్యోతి పత్రికల యాజమాన్యాలపై ... ఆగ్రహం వ్యక్తం చేశారు."
  — Sakshi YS Jagan PC Seci Sak 29112024 3.jpeg (2024-11-28)
```

Entity resolution works: `SECI` merged with "Solar Energy Corporation of India
Limited"; `చంద్రబాబు` merged into "N. Chandrababu Naidu".

**Acceptance met:** `timeline "TTD"` spans meets **3 → 4 → 5 → 22** in correct
date order.

---

## Verification (all free)

```bash
source .venv/bin/activate
python -c "import kng.graph.ontology, kng.store.graph, kng.pipeline.graph_build, \
           kng.pipeline.graph_extract, kng.graph_query"
python -m kng.graph.ontology                                # 11 types, 13 relations
python -m kng.pipeline.run --stage graph --plan-only        # cost report, 0 calls
python -m kng.pipeline.run --stage graph --structural-only  # 698 nodes / 714 edges
python -m kng.graph_query stats
python -m kng.stats
python -m kng.query "Tirupati laddu" -k 2                   # WP2 still works
```

`KNG_FAKE_LLM=1` exercises the **entire paid path** offline — selection, cache,
concurrency, resume, validation, resolution, communities. Verified: full corpus
2950 extractions; re-run with `--force` made **0 new calls**; a no-op re-run does
not zero `index/stats.json`. Delete `index/graph/extractions/` fixtures
afterwards (they are fingerprinted, so a real run ignores them anyway).

---

## Cluster → local hand-off

Extraction runs on the **cluster**; WP4/WP5 run **locally**. `index/` and
`extracted/` are now **committed to git**, so the hand-off is just a clone.

| path | files | size | in git? |
|---|---|---|---|
| `index/lancedb/` | 3269 | 147 MB | yes |
| `index/chunks/` | 635 | 35 MB | yes |
| `index/graph/` | 192 | 3.9 MB (grows with extraction) | yes |
| `index/manifest.json`, `stats.json` | 2 | 0.4 MB | yes |
| `extracted/` | 645 | 18 MB | yes — citations resolve against it |
| `/data/` | — | 584 MB | **no** — source archive, not needed to query |
| `.env` | — | — | **no** — secret |

```bash
git clone <repo> KNG && cd KNG
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[local]'      # bge-m3 for query-side embedding
cp .env.example .env           # SARVAM_API_KEY only needed to resume WP3

# smoke test — none of these need an API key
python -m kng.graph_query stats
python -m kng.graph_query entities --type Person --top 10
python -m kng.graph_query timeline "TTD"
python -m kng.query "Tirupati laddu" -k 5
python -m kng.stats
```

**No `SARVAM_API_KEY` is needed to query** — WP4 development is entirely free of
the paid-call guardrail. The first `kng.query` downloads bge-m3 (~2 GB) from
HuggingFace; after that everything runs offline.

Local dependencies: `pyyaml`, `networkx`, `pydantic`, `lancedb`, `pyarrow`, plus
`.[local]` for bge-m3.

**`.gitignore` caveat.** The rule is `/data/`, anchored to the repo root. An
unanchored `data/` matches *any* directory of that name at any depth, so it also
excluded `extracted/data/` and `index/chunks/data/` — 2100 of 4743 artifact
files would have been silently missing from a clone. Verify after any
`.gitignore` change:

```bash
for d in index/lancedb index/chunks index/graph extracted; do
  printf "%-18s disk=%s staged=%s\n" "$d" \
    "$(find $d -type f | wc -l)" \
    "$(git ls-files -z -- $d | tr '\0' '\n' | grep -c .)"
done
```
(Use `-z`: git quotes paths containing spaces and Telugu characters, which
breaks a naive `grep "^$d/"`.)

**Trade-off of committing the index.** LanceDB files are binary and rewritten
wholesale, so each re-index adds another full ~180 MB copy to git history that
git cannot delta. The repo is currently ~582 MB including history. If it becomes
unwieldy, re-ignore `index/` and `extracted/` and use `scripts/package_index.sh`,
which tars both with a checksum.

To top up the graph after more extraction, commit `index/graph/` — it is the only
directory that changes.

---

## Known gaps / what WP4 should pick up

- ~~Extraction is incomplete~~ — **resolved 2026-07-26.** All 4251/4251 units
  extracted, 1 unrecoverable failure (already retried with `--retry-split`,
  reply still didn't parse — not worth chasing further at 1/4251).
- ~~Community summaries have not run~~ — **resolved.** 22 of 1157 communities
  are summarised (the rest are below `min_size=3`); confirmed complete, not
  partial.
- **`--retry-split N`** re-runs only chunks absent from the cache at a smaller
  size, merging pieces back under the parent hash so it stays idempotent.
- **Publication metadata is thin** — only Sakshi (268 chunks) and Eenadu (7) are
  tagged across 567 news-clip chunks. A WP1 metadata gap; `COVERED_BY` stays
  sparse until fixed.
- **Three `press_meet_id`s are filename fallbacks** with no date; excluded from
  the `PRECEDES` chain and reported as a diagnostic.
- **Entity resolution does no fuzzy matching by default** — only the ontology
  alias table, the extractor's `english_name`, and exact normalised match.
  Wrongly merging two politicians misattributes quotes, which is worse than a
  duplicate node. Extend `config/ontology.yaml`'s alias table after reading real
  output.
- ~~No test suite~~ — **resolved.** `tests/test_graph_cache.py` covers cache
  fingerprints, source-type scope and malformed-model-output handling; 34 tests
  total across `tests/`.
- **The Neo4j mirror is written but untested** — `neo4j` is not installed.
- **`LLM_REASONING_EFFORT=null` closed the tier gap that mattered.** The
  4096-token cap no longer forces chunk splitting or truncation repair (0
  salvaged across all 4251 units) — see "the two levers" above. A tier upgrade
  would still cut wall-clock via higher RPM, but is no longer needed for
  extraction completeness.
