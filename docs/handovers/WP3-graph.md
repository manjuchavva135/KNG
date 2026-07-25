# WP3 — Knowledge graph build

**Status:** code complete and verified. The paid extraction pass is **running on
the cluster and incomplete** — resumable, re-bills nothing. Everything else
(structural graph, resolution, communities, query CLI) is done and free to run.

---

## Quick resume

```bash
cd ~/KNG && source .venv/bin/activate

# is the extraction still going?
pgrep -af "kng.pipeline.run --stage graph"
tail -5 graph.log

# resume it (identical command; already-extracted chunks are never re-billed)
nohup .venv/bin/python -m kng.pipeline.run --stage graph --concurrency 12 > graph.log 2>&1 &

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

Diagnostic that settles "hung or just slow?" in one command:

```bash
for t in $(ls /proc/<pid>/task); do cat /proc/<pid>/task/$t/wchan; echo; done | sort | uniq -c
# all poll_schedule_timeout + low CPU  -> blocked on dead sockets
```

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
| graph — selection | 4251 extraction units (2950 chunks + 1301 split pieces) |
| **graph — extraction (paid, IN PROGRESS)** | **1043 records · 11,429 entities · 2,721 relations** |

Sample of the enriched graph (SECI meet + partial corpus): 527 nodes / 592 edges
— Organization 116, Claim 113, Scheme 104, Person 55, Place 43, Issue 27.

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

Extraction should finish on the **cluster**; WP4/WP5 run **locally**.

| path | size | ship? |
|---|---|---|
| `index/lancedb/` | 147 MB | yes |
| `index/chunks/` | 35 MB | yes |
| `index/graph/` | 3.9 MB (grows with extraction) | yes |
| `index/manifest.json`, `stats.json` | 0.4 MB | yes |
| `extracted/` | 18 MB | yes — citations resolve against it |
| **total** | **~204 MB** | |

```bash
# ── on the cluster ──
cd ~/KNG
tar -czf kng-index-$(date +%Y%m%d).tar.gz index/ extracted/
sha256sum kng-index-*.tar.gz | tee kng-index.sha256
du -sh index extracted

# ── on the laptop ──
git clone <repo> KNG && cd KNG
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[local]'                  # bge-m3 for query-side embedding
sha256sum -c kng-index.sha256 && tar -xzf kng-index-*.tar.gz
cp .env.example .env                       # SARVAM_API_KEY only needed for WP3

# post-copy smoke — none of these need an API key
python -m kng.graph_query stats
python -m kng.graph_query entities --type Person --top 10
python -m kng.graph_query timeline "TTD"
python -m kng.query "Tirupati laddu" -k 5
python -m kng.stats
```

Local dependencies: `pyyaml`, `networkx`, `pydantic`, `lancedb`, `pyarrow`, plus
`.[local]` for bge-m3. **No `SARVAM_API_KEY` is needed to query the graph** — WP4
development is guardrail-free.

To top up the graph later, re-run extraction on the cluster, then re-copy
`index/graph/`. It is the only directory that changes.

---

## Known gaps / what WP4 should pick up

- **Extraction is incomplete.** ~1043 of 4251 units done. Resume with the command
  at the top; nothing is re-billed.
- **`--retry-split N`** re-runs only chunks absent from the cache at a smaller
  size, merging pieces back under the parent hash so it stays idempotent. Use it
  after the main pass to recover failures.
- **Community summaries (phase E) have not run** — they are the last paid step
  (~50–200 calls) and produce the "god-node" titles WP4's global queries read.
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
- **No test suite.** The pure logic added here (JSON repair, name normalisation,
  triple validation, chunk selection, `_halve`) is the cheapest place to start.
- **The Neo4j mirror is written but untested** — `neo4j` is not installed.
- **A tier upgrade is the highest-leverage remaining fix**: the 4096-token output
  cap is what forces chunk splitting, truncation repair, and the retry pass. Pro
  (60 req/min) would also cut wall-clock.
