# WP6 — Eval harness, hardening, portable export

**Status:** eval harness and portable export ✅ done and verified 2026-07-26.
**120 tests pass** (79 from WP1–WP5 + 22 eval + 19 export), all offline. Retrieval
quality now has a baseline number; the reranker and the cross-script gap are
scoped but not built.

---

## Why this WP exists

Every measured decision in this project paid for itself — `scripts/bench_extract.py`
turned a 17-hour graph pass into 73 minutes by testing four configurations instead
of arguing about them. Answer quality had no such instrument. WP5 recorded
`uncited_sentences` and `invalid_citations` per answer into `var/queries.jsonl`,
but nothing ran a *fixed* set of questions, so "did that change help?" was
unanswerable.

## What was built

| file | role |
|---|---|
| `kng/eval/questions.yaml` | 30 questions grounded in the real archive — 22 English, 8 Telugu, tagged `single` / `cross` / `temporal` / `numeric` |
| `kng/eval/harness.py` | load, validate, score, aggregate, render markdown, save JSON |
| `kng/eval/__main__.py` | `python -m kng.eval` — ablations, `--only`, `--baseline`, and the spend gate |
| `kng/pipeline/export.py` | `--plan` / build / `--verify` / `--extract`, with an in-archive `EXPORT.json` |
| `kng/store/vector.py` | `table_dim` + `check_dim` — refuses a query embedded by the wrong model |
| `kng/config.py` | default `LOCAL_EMBED_MODEL` corrected to `BAAI/bge-m3` (see "Bugs found") |
| `tests/test_eval.py`, `tests/test_export.py` | 41 cases, no index or model needed |
| `docs/eval/baseline-2026-07-26-k8.md` | the committed baseline (`var/eval/` is git-ignored) |

## How to run it

```bash
python -m kng.eval                       # free: retrieval scores, no key, no network
python -m kng.eval -k 30                 # ablate k
python -m kng.eval --no-graph            # ablate the graph leg
python -m kng.eval --only laddu-en,seci-tariff
python -m kng.eval --baseline docs/eval/baseline-2026-07-26-k8.json   # deltas
python -m kng.eval --validate            # every expectation names a real press meet
KNG_FAKE_LLM=1 python -m kng.eval --answer   # end-to-end offline, still free
python -m kng.eval --answer --spend      # PAID: one Sarvam call per question
```

Reports land in `var/eval/<ts>-k8.json` + `.md`. Every metric prints with a delta
when `--baseline` is given, which is the whole point: a change is judged against a
recorded number, not a memory of one.

## What is measured, and what is deliberately not

**Expectations are per press meet, not per chunk.** Nobody hand-labelled 4267
passages. A chunk-level gold set invented here would look precise and measure the
invention. So a question expects *the right press meet* to appear among the
retrieved passages:

- `meet_hit` / hit rate — did any retrieved passage come from an expected meet
- `mrr` — 1 / rank of the first such passage (ranking quality, not just recall)
- `meet_recall` — of the expected meets, how many were represented
- `meet_precision` — informational only: a news clip from another meet on the same
  topic is not wrong, so this is not a target
- `entity_hit` — did the graph leg link the entity the question names
- answer mode adds `cited`, `invalid_citations`, `uncited_sentences`, and
  `cited_expected_meet` — whether a *cited* source came from an expected meet,
  which is the strongest single signal in the harness

`--validate` refuses to run if a question expects a press meet the index does not
have, because a stale expectation would read as a permanent retrieval failure.

## Baseline — 2026-07-26, k=8, graph on, no reranker

| metric | value |
|---|---:|
| questions | 30 |
| meet hit rate | **0.667** |
| MRR | **0.528** |
| meet recall | 0.526 |
| entity link rate | 0.867 |
| mean graph facts | 17.4 |
| mean retrieval | 1.24 s |

| cut | n | hit rate | MRR |
|---|---:|---:|---:|
| cross-meet | 11 | 0.818 | 0.700 |
| numeric | 2 | 1.000 | 0.750 |
| single-meet | 16 | 0.562 | 0.415 |
| temporal | 1 | 0.000 | 0.000 |
| **English** | 22 | **0.545** | 0.391 |
| **Telugu** | 8 | **1.000** | 0.906 |

### The two findings worth acting on

**1. It is a ranking problem, not a coverage problem.** At `k=30` the hit rate
rises to **0.867 (+0.200)** while MRR barely moves (**+0.030**). The right press
meet is already in the top 30 and not in the top 8 — precisely the case a
cross-encoder reranker fixes. That was the WP5 handover's top-listed gap, and it is
now measured rather than assumed.

**2. English questions score half what Telugu ones do** (0.545 vs 1.000). Reading
the misses says why: English queries land on the third-party newspaper PDFs
(`Vijayawada_NIE_*.pdf`, `Vijayawada_DC_*.pdf` — 2995 `source_doc` chunks, the
largest and most English part of the corpus), which outrank the press-meet material
they were filed as evidence for. Telugu questions cannot match those pages, so they
go straight to the transcripts. This is a weighting problem, not a language-model
problem: `source_doc` should not compete on equal terms with a press release from
the meet itself.

Both are WP6 work that this handover explicitly does *not* claim to have done.

## Portable export

```bash
python -m kng.pipeline.export --plan                    # inventory + sizes, writes nothing
python -m kng.pipeline.export --out kng-index.tar.gz    # archive + .sha256
python -m kng.pipeline.export --verify kng-index.tar.gz
python -m kng.pipeline.export --extract kng-index.tar.gz --dest /path/to/root
```

**Allow-list, not deny-list.** `index/manifest.json`, `index/stats.json`,
`index/chunks`, `index/lancedb`, `index/graph`, `extracted`, `config/ontology.yaml`.
A deny-list quietly grows holes; an allow-list fails closed. `.env`, `var/` and
`data/` are therefore excluded structurally, and a test asserts the API key's value
does not appear anywhere in the archive bytes.

**`EXPORT.json` travels inside the archive** with counts, git commit, graph size,
and — the load-bearing field — the embedding model and dimension. `--verify`
re-reads the archive, checks the sidecar checksum, compares the file count against
the record, refuses unsafe member paths, and warns when the receiving machine is
configured for a different embedding model.

Measured on this corpus: **5140 files · 197.9 MB → 66.8 MB compressed in 22 s.**

### Real round trip, not just unit tests

1. `--out` → 66.8 MB archive + `.sha256`
2. `sha256sum -c` → OK; `--verify` → *"OK — archive matches its record"*, reporting
   8120 nodes / 10773 edges, 635 chunk files, `BAAI/bge-m3`
3. `--extract` into a directory holding **only a copy of `kng/`** — no `index/`, no
   `extracted/`, **no `.env`** — then queried it:
   - `python -m kng.graph_query stats` → 8120 nodes / 10773 edges
   - `python -m kng.query "Tirupati laddu" -k 2` → ranked, cited passages

Step 3 is what caught the bug below. A unit test on synthetic data would not have.

## Bugs found

**The documented clone-and-run path was broken.** A fresh clone has no `.env`, and
the `LOCAL_EMBED_MODEL` default in `kng/config.py` was still
`intfloat/multilingual-e5-base` — the 768-dim model WP2 abandoned for silently
truncating at 512 tokens. The committed index is bge-m3 at 1024 dims, so the first
query embedded its question with the wrong model and died inside LanceDB with:

```
ValueError: There is no vector column in the data. Please specify the vector
column name for vector search
```

which sends the reader hunting for a missing column that is right there. The real
cause is that LanceDB infers the vector column *by matching the query's dimension*,
and nothing matched 768. Two fixes:

1. the default is now `BAAI/bge-m3`, the model that actually built the index;
2. `vector.check_dim` compares the query's dimension to the table's before
   searching and raises a message naming both dimensions, the configured model, and
   `LOCAL_EMBED_MODEL`.

The second matters more than the first. **Had the two models shared a dimension
there would have been no error at all** — just confidently ranked nonsense, with
citations, in a politically sensitive archive. Loud failure is the feature.

*(Smaller: `_counts` read the repo's LanceDB store even when exporting a different
root, so an archive could have described the wrong vector table. It now only
reports vectors when exporting the configured project root.)*

## Verification performed

| check | result |
|---|---|
| full suite | **120 pass** (`python -m unittest discover -s tests`) |
| eval tests | 22, offline, 0.10 s — no index, no model, no network |
| export tests | 19, on a synthetic root |
| `--validate` on the shipped set | 30 questions, every expectation resolves |
| free baseline | 30 questions scored, 0 errors, report written |
| k=30 ablation vs baseline | hit +0.200, MRR +0.030 (recorded above) |
| spend gate | `--answer` against a real provider without `--spend` → exit 2 |
| archive build | 5140 files · 197.9 MB → 66.8 MB · 22 s |
| `sha256sum -c` · `--verify` | OK · "archive matches its record" |
| tampered checksum · missing sidecar · model mismatch · no record | each reported |
| tarball traversal (`../escaped.txt`) | refused by `--extract`, flagged by `--verify` |
| secrets in archive | `.env`, `var/`, `data/` absent; key value absent from the bytes |
| extracted copy, no `.env` | `graph_query stats` and `kng.query` both work |
| mismatched `LOCAL_EMBED_MODEL` | actionable `ValueError`, not LanceDB's opaque one |

## What WP6 still has open

- **Reranker.** `RERANK_PROVIDER=none`. A cross-encoder over the fused top-30 is
  the measured next step — baseline and command are both ready, so the change can
  be judged with `--baseline` instead of by feel.
- **`source_doc` weighting.** The dominant English failure mode. Options: a
  per-source-type prior in RRF, or excluding `source_doc` from the vector leg while
  keeping it searchable. Either needs an eval run, not an opinion.
- **Cross-script fact relevance.** `graph_context._relevance` scores an English
  question at zero against Telugu evidence quotes; `text_en` is unpopulated.
- **Ontology merge.** `TTD` exists as both `Place` and `Organization`.
- **Answer-mode baseline.** Not run — that is 30 paid calls and the user's call.
  `KNG_FAKE_LLM=1 python -m kng.eval --answer` exercises the path for free.
- **`ANSWER_REASONING_EFFORT`** still un-A/B'd for synthesis (WP3 measured it only
  for extraction). The harness makes this a two-command comparison now.
