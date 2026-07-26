# WP4 — GraphRAG query engine (hybrid retrieval → cited synopsis)

**Status:** ✅ code done and verified free-of-charge on 2026-07-25.
Retrieval is fully exercised against the real index; synthesis is exercised with
the offline fixture provider (`KNG_FAKE_LLM=1`). The one paid step — a single
LLM call per question — has never been run from this machine.

Built on a laptop clone of the repo, which is the hand-off this WP exists to
prove: `git clone` → `pip install -e '.[local]'` → answers, with **no
`SARVAM_API_KEY` and no reprocessing**.

---

## What was built

| file | role |
|---|---|
| `kng/retrieval/hybrid.py` | dense (bge-m3 / LanceDB) + BM25 legs, SQL provenance prefilters, Reciprocal Rank Fusion, duplicate collapsing |
| `kng/retrieval/graph_context.py` | question → graph entities → neighbourhood facts, timeline, communities; relevance scoring |
| `kng/retrieval/__init__.py` | `retrieve()` → a `Context` holding passages + facts + timeline + diagnostics |
| `kng/generation/synthesize.py` | one numbering scheme over passages *and* facts, grounded prompt, `answer()`, post-hoc citation verification |
| `kng/answer.py` | `python -m kng.answer` CLI — the user-facing command |
| `tests/test_wp4.py` | 20 `unittest` cases over the pure logic (no index, no model, no network) |
| `tests/test_graph_cache.py` | 14 cases over cache fingerprints, source-type scope, and malformed model output |
| `scripts/bench_extract.py` | measures extraction configs (model × reasoning effort) on real chunks before a long paid pass |
| `kng/providers/llm.py` | `FakeLLM.complete` now returns a deterministic extractive answer, so the whole synthesis path runs offline |

## How to run it

```bash
python -m kng.answer "What did Jagan say about the Tirupati laddu adulteration?"
python -m kng.answer "SECI solar tariff" -k 12 --since 2024-01-01 --chars 300
python -m kng.answer "ఏపీ మద్యం కుంభకోణం" --lang te            # answers in Telugu
python -m kng.answer "TTD laddu" --retrieval-only               # free: shows the evidence
python -m kng.answer "TTD laddu" --retrieval-only --prompt      # free: shows the exact prompt
KNG_FAKE_LLM=1 python -m kng.answer "TTD laddu"                 # free: whole path, no model
python -m unittest discover -s tests
```

Flags that matter: `--no-graph` (passages only), `--no-keyword` / `--no-vector`
(one leg), `--hops 2` (wider graph expansion), `--json` (machine-readable),
`--all-sources` (also list retrieved sources the answer did not cite),
`--build-fts` (one-off, free — builds the BM25 index if a clone lacks it).

## Key decisions

**Reciprocal Rank Fusion, not score blending.** A cosine distance and a BM25
score share no scale, and any weighting between them would be tuned on this
corpus and wrong on the next. RRF ranks by `Σ 1/(60 + rank)`, so a passage both
legs found beats one that either leg alone ranked first, and the system degrades
cleanly to a single leg when the other returns nothing (or is missing entirely).

**Passages and graph facts share one `[n]` sequence.** A reader wants the file,
page and date; whether the claim reached the answer through a chunk or an edge
is an implementation detail. Facts are numbered after passages because a fact is
a compressed restatement of something a passage says verbatim.

**Citations are verified, not trusted.** `verify_citations` strips any `[n]`
outside the source list and counts sentences that carry no citation at all.
A hallucinated `[9]` is invisible to a reader and fatal to the project's premise,
so it is removed and reported (`warning: stripped 1 citation(s)…`) rather than
left to look authoritative.

**Entity linking is lexical, deliberately.** N-grams of the question (longest
span first) are matched against the graph's own names, its alias lists, and the
ontology alias table. Embedding-based linking would bind "Lokesh" to the wrong
Lokesh; misattributing a quote to the wrong politician is the one failure this
archive cannot afford — the same reasoning as WP3's refusal to fuzzy-match.

**Hub entities are filtered by question relevance.** Jagan's node has hundreds
of edges, so "his top-weighted edges" answers whatever he talks about most, not
what was asked. Facts are scored by term overlap with the question **after
removing the terms that linked the entity** — otherwise every Jagan edge scores
identically on the word "Jagan". Off-topic edges are dropped once ≥3 on-topic
ones survive; if none match (common when the question is English and the
evidence Telugu) the six strongest are kept as background instead of flooding
the prompt with a neighbourhood.

**Retrieval never raises for a missing leg.** No FTS index, no graph file, no
embedding model — each is reported in `diagnostics` and skipped. The graph is
*expected* to be partial until WP3's paid pass finishes, and an answer from
passages alone with honest provenance beats an exception.

## Verification (all free, nothing billed)

```
python -m unittest discover -s tests          20 tests, OK
python -m kng.query "Tirupati laddu" -k 2     WP2 path unchanged
```

| check | result |
|---|---|
| imports (incl. `kng.retrieval`, `kng.generation`, `kng.answer`) | clean |
| BM25 leg alone, `--no-vector --no-graph` | 20 hits; LiveLaw + ThePrint laddu coverage |
| hybrid, `-k 6` | 6 passages fused from vector 20 + keyword 20 → 29 unique |
| graph leg, "SECI solar power agreement" | 3 entities linked, 8 facts with Telugu evidence quotes |
| graph leg, "ఈనాడు ఆంధ్రజ్యోతి క్షమాపణ" | 2 entities linked, 6 facts, all matched the question |
| date prefilter `--since 2024-09-01 --until 2024-10-31` | only in-range passages returned |
| `KNG_FAKE_LLM=1` end-to-end | answer rendered, 4 of 11 sources cited, citation verification and warnings fired |
| latency | cold 13.8 s (bge-m3 load), **warm 0.22 s/query** |

Index counts as of 2026-07-26 (post full-corpus extraction): **4267 chunk rows**
in `index/lancedb` (FTS index `text_idx` present) and **8120 nodes / 10773 edges
/ 1157 communities** in `index/graph`.

## Known gaps / what WP5 should pick up

- ~~The committed graph is partial~~ — **resolved 2026-07-26, twice-over.**
  WP3's speech-first pass first brought it to 4803 nodes across all 33 meets;
  the remaining `source_doc` (evidence-PDF) units were then extracted too,
  bringing it to the **full-corpus** counts above — 4251/4251 units, 1 known
  failure. Verified: `neighbors "Jagan"` returns 120 edges including
  `ACCUSES → N. Chandrababu Naidu ×37` spanning 2024-07-26→2026-05-21 with Telugu
  evidence quotes, and `kng.answer` on the laddu question links 3 entities and 17
  question-matching facts plus a 3-meet timeline.
- ~~Community summaries are absent~~ — **resolved.** 22 of 1157 communities are
  summarised; the rest are singletons/pairs below phase E's `min_size=3`, so this
  is complete rather than partial. The diagnostic now distinguishes "phase E never
  ran" from "these clusters are too small to summarise".
- ~~2528 `source_doc` units are unextracted~~ — **resolved.** Extracted in a
  second pass at the same `LLM_REASONING_EFFORT=null` config, ~26 units/min
  (faster than the speech units, which average longer). Nothing from the first
  pass was re-billed.
- **Fact relevance is lexical and does not cross scripts.** An English question
  scores zero against Telugu evidence quotes. The `text_en` column exists but is
  unpopulated; translating evidence at index time, or scoring facts with bge-m3,
  is the fix.
- **No reranker.** `RERANK_PROVIDER=none`. A cross-encoder over the fused top-30
  is the obvious next quality step, and the seam is already there.
- **The paid synthesis call is unexercised.** Prompt sizes measured ~8.7k chars;
  `sarvam-105b` on the starter tier caps output at 4096 tokens, and WP3's
  measured lesson (`reasoning_effort="low"`) has not been re-measured for
  synthesis. First real run should compare `low` vs `null` on answer quality.
- **No answer-quality eval.** WP6's harness should score citation coverage
  (`uncited_sentences`, `invalid_citations` are already computed per answer) over
  a fixed question set.
