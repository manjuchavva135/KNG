"""Phases B, C and E of the graph build — the parts that cost money.

  B. one forced-tool-call LLM extraction per chunk, cached on disk;
  C. entity resolution, folding surface forms onto one node per real thing;
  E. one LLM summary per community, the "god nodes" global queries read.

**The cache is the point of this module.** Extraction results are stored under
`index/graph/extractions/<source file>.json` keyed by the chunk's
`content_hash`, never by `chunk_id`. That choice buys three things at once: a
killed run resumes at chunk granularity instead of restarting; re-chunking, which
is deterministic, does not invalidate work already paid for; and the archive's
byte-identical duplicate files collapse onto one call each — 253 of 4267 chunks
in this corpus, paid for once.

Nothing here runs unless the caller asks for it. `--structural-only` skips the
module entirely, so the graph stage stays verifiable without an API key.
"""
from __future__ import annotations

import json
import re
import sys
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Optional

from ..config import ROOT, settings
from ..graph import ontology as onto
from ..models import Chunk, Entity, Relation
from ..store import graph as gstore

# The starter tier caps sarvam-105b output at 4096 tokens, and the model spends
# 2500-4000 of them reasoning before it writes anything. Ask for just under the
# ceiling; `parse_json_object` repairs whatever still gets cut off.
MAX_OUTPUT_TOKENS = 4000

# Latin script test for picking a display name: the corpus mixes Telugu and
# English spellings of the same person, and citations read better in Latin.
_LATIN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")

_SYSTEM = """You extract a knowledge graph from Andhra Pradesh political press-meet material.
The text may be Telugu, English, Hindi, or a mix of them. Read it in whatever language it is written.

NODE TYPES you may use:
{types}

RELATION TYPES you may use (source type -> target type):
{relations}

Rules:
1. Extract only what the passage actually states. Never infer, complete from world knowledge, or guess.
2. Keep each entity's `name` exactly as written in the passage. If it is in Telugu or Hindi script, also give the usual English spelling in `english_name`.
3. Every `source` and `target` in `relations` must be the `name` of an entity you listed in `entities`.
4. A Claim is one specific allegation or promise, stated as a short sentence in the passage's own language.
5. An Issue is a recurring controversy or topic thread (e.g. the TTD laddu adulteration row), not a one-off remark.
6. `evidence` must be a short verbatim quote from the passage.
7. If the passage is boilerplate, a list of headings, or names nothing, return empty lists. Empty is a correct answer.
"""


# ── chunk selection: the cost gate ─────────────────────────────────────────────
_TAG = re.compile(r"<[^>]{1,40}>")


def _tag_density(text: str) -> float:
    return sum(len(m.group()) for m in _TAG.finditer(text)) / max(1, len(text))


def select_chunks(chunks_by_file: dict[str, list[Chunk]]) -> tuple[list[tuple[str, Chunk]], Counter]:
    """Decide which chunks are worth an LLM call. Free and deterministic.

    Not every chunk is prose. One file in this corpus — a DSC merit list —
    is 598 chunks of `<td>` candidate rows, and a second is 152 more; sent
    blind they would be ~750 paid calls that return thousands of junk `Person`
    nodes and bury the real graph. Three cheap rules remove them:

      * outside `GRAPH_SOURCE_TYPES` when that is set — scope, see below;
      * shorter than `GRAPH_MIN_CHUNK_CHARS` — cover pages and slide titles;
      * markup denser than `GRAPH_MAX_TAG_DENSITY` — table dumps;
      * more than `GRAPH_MAX_CHUNKS_PER_FILE` chunks from one file.

    `GRAPH_SOURCE_TYPES` exists because the corpus is 59% `source_doc` — PDFs
    filed *as evidence* (court orders, SECI tariff sheets, DSC merit lists)
    rather than anything Jagan said. Extracting
    `press_release,news_clip,video,slide` first yields the graph the project's
    questions actually need at 40% of the calls, and the rest can be added later
    at content-hash granularity with nothing re-billed.

    Skipped chunks are *not* invisible: their file remains a `Source` node with
    its `CITES_SOURCE` edge, and they stay fully searchable in LanceDB. They are
    only excluded from the paid pass. The thresholds sit near real content
    (a budget deck's tables measure ~0.5 too), so `--plan-only` prints the
    selection and the files it trims before any money is spent.
    """
    s = settings()
    picked: list[tuple[str, Chunk]] = []
    skipped: Counter = Counter()
    seen: set[str] = set()
    wanted = {t.strip() for t in s.graph_source_types.split(",") if t.strip()}

    for rel, chunks in sorted(chunks_by_file.items()):
        kept_here = 0
        for c in chunks:
            if wanted and c.source_type not in wanted:
                skipped["source_type"] += 1
                continue
            if len(c.text) < s.graph_min_chunk_chars:
                skipped["short"] += 1
                continue
            if _tag_density(c.text) >= s.graph_max_tag_density:
                skipped["markup"] += 1
                skipped[f"file:{rel}"] += 1
                continue
            if kept_here >= s.graph_max_chunks_per_file > 0:
                skipped["over_file_cap"] += 1
                continue
            key = c.content_hash or c.chunk_id
            if key in seen:
                skipped["duplicate"] += 1
                continue
            seen.add(key)
            for piece in _split_for_extraction(c, s.graph_max_chunk_chars):
                picked.append((rel, piece))
                if piece.chunk_id != c.chunk_id:
                    skipped["split_pieces"] += 1
            kept_here += 1
    return picked, skipped


def _split_for_extraction(c: Chunk, limit: int) -> list[Chunk]:
    """Slice an over-long chunk into extraction-sized pieces.

    Measured on the SECI meet: calls that failed had a median input of 3360
    chars against 2064 for calls that succeeded. The model reasons in proportion
    to what it is given, and past roughly 2.5k chars it exhausts the tier's
    4096-token output cap before emitting anything — a call billed for nothing.

    Pieces keep the parent's citation, meet and date, so a mention found in one
    still resolves to exactly the same source. Only `chunk_id` and
    `content_hash` differ, and the hash is of the piece, which keeps the cache
    key honest.
    """
    if limit <= 0 or len(c.text) <= limit:
        return [c]
    import hashlib

    pieces: list[Chunk] = []
    text = c.text
    start = 0
    idx = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):                       # prefer a sentence/line break
            window = text.rfind("\n", start + limit // 2, end)
            if window == -1:
                window = text.rfind(" ", start + limit // 2, end)
            if window > start:
                end = window
        body = text[start:end].strip()
        if body:
            p = c.model_copy()
            p.text = body
            p.text_original = body
            p.chunk_id = f"{c.chunk_id}#s{idx}"
            p.content_hash = hashlib.sha1(
                " ".join(body.split()).encode("utf-8")).hexdigest()
            pieces.append(p)
            idx += 1
        start = end
    return pieces or [c]


def plan_report(chunks_by_file: dict[str, list[Chunk]], files: list[Path]) -> dict:
    """What a paid run would cost, without making a single call."""
    picked, skipped = select_chunks(chunks_by_file)
    cached = load_all_cached(files)          # any fingerprint: an estimate
    to_bill = [c for _, c in picked if (c.content_hash or c.chunk_id) not in cached]
    chars = sum(len(c.text) for c in to_bill)
    total = sum(len(v) for v in chunks_by_file.values())
    return {
        "chunks_total": total,
        "chunks_selected": len(picked),
        "already_cached": len(picked) - len(to_bill),
        "calls_to_bill": len(to_bill),
        "content_chars": chars,
        "est_input_tokens": chars // 3 + len(to_bill) * 700,
        "est_output_tokens": len(to_bill) * 400,
        "skipped": {k: v for k, v in skipped.items() if not k.startswith("file:")},
        "trimmed_files": dict(Counter(
            {k[5:]: v for k, v in skipped.items() if k.startswith("file:")}).most_common(5)),
    }


# ── cache ──────────────────────────────────────────────────────────────────────
def extractions_dir() -> Path:
    return gstore.graph_dir() / "extractions"


def cache_path(rel: str) -> Path:
    return extractions_dir() / (rel + ".json")


# Bumped whenever the extraction prompt or schema changes meaningfully, so a
# stale cache is invalidated visibly rather than silently reused.
# g2: tool_choice auto + 4000-token budget + truncation repair. g1 entries were
# produced under `tool_choice="required"`, which sarvam-105b answers with an
# empty message, so they are near-empty and must not be reused.
PROMPT_VERSION = "g2"


def cache_fingerprint(llm) -> str:
    """Identifies what produced a cache entry: provider, model, prompt version.

    Written into every cache file and checked on load. Without it, extractions
    produced by the offline `FakeLLM` would look exactly like paid results and a
    real run would skip those chunks as "already done" — silently shipping
    fixture data as the knowledge graph.
    """
    return f"{type(llm).__name__}/{getattr(llm, 'model', '?')}/{PROMPT_VERSION}"


def _fp_acceptable(fp: str | None) -> bool:
    """True when a fingerprint denotes reusable *paid* output.

    Deliberately model-agnostic: `sarvam-105b` and `sarvam-30b` records answer
    the same prompt about the same passage, so a later run on a different model
    must not re-bill thousands of chunks a previous one already paid for. What it
    does enforce is the standing guardrail — `FakeLLM` fixture records can never
    be reused by a real run — and that the prompt version matches, since a
    changed prompt changes what the record means.
    """
    if not fp:
        return False
    parts = fp.split("/")
    if len(parts) != 3:
        return False
    provider, _model, version = parts
    if provider.startswith("Fake"):
        return False
    return version == PROMPT_VERSION


def _reusable(entry_fp: str | None, current_fp: str) -> bool:
    """Whether a cache entry may be used by the run identified by `current_fp`."""
    if not entry_fp:
        return False
    if entry_fp == current_fp:               # same run config: always fine
        return True
    if not _fp_acceptable(current_fp):       # a fixture run reuses only its own
        return False
    return _fp_acceptable(entry_fp)


def load_cache(rel: str, fingerprint: str | None = None) -> dict[str, dict]:
    """Cached records for one file, dropping entries this run may not reuse.

    Filtering is **per record**, not per file. It used to be per file: one
    fingerprint at the top, any mismatch discarding everything under it. That
    made changing model or reasoning effort cost a re-extraction of every chunk
    already paid for — 1183 units at the time this changed — which is the
    opposite of what the cache exists for.
    """
    fp = cache_path(rel)
    if not fp.exists():
        return {}
    try:
        blob = json.loads(fp.read_text(encoding="utf-8"))
    except ValueError:                       # a run killed mid-write
        print(f"  corrupt cache, re-extracting: {rel}", file=sys.stderr)
        return {}
    if not isinstance(blob, dict) or "records" not in blob:
        return {}                            # pre-fingerprint layout: discard
    records = blob.get("records") or {}
    if fingerprint is None:
        return records
    file_fp = blob.get("fingerprint")
    out: dict[str, dict] = {}
    for key, rec in records.items():
        entry_fp = (rec.get("_fp") if isinstance(rec, dict) else None) or file_fp
        if not _reusable(entry_fp, fingerprint):
            continue
        # Stamp what actually produced it, so a later save cannot relabel an
        # older model's record as this run's.
        if isinstance(rec, dict) and not rec.get("_fp") and entry_fp:
            rec = {**rec, "_fp": entry_fp}
        out[key] = rec
    return out


def save_cache(rel: str, records: dict[str, dict], fingerprint: str) -> None:
    """Write atomically — a kill during the write must not lose paid results."""
    fp = cache_path(rel)
    fp.parent.mkdir(parents=True, exist_ok=True)
    stamped: dict[str, dict] = {}
    for key, rec in records.items():
        if isinstance(rec, dict) and not rec.get("_fp"):
            rec = {**rec, "_fp": fingerprint}
        stamped[key] = rec
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    tmp.write_text(json.dumps({"fingerprint": fingerprint, "records": stamped},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(fp)


def load_all_cached(files: Iterable[Path],
                    fingerprint: str | None = None) -> dict[str, dict]:
    """Every cached extraction, keyed by content_hash, across all files.

    Loaded up front so a duplicate passage in a *different* source file is
    recognised as already paid for.
    """
    memo: dict[str, dict] = {}
    for path in files:
        memo.update(load_cache(str(path.relative_to(ROOT)), fingerprint))
    return memo


# ── phase B: extraction ────────────────────────────────────────────────────────
def _user_prompt(chunk: Chunk) -> str:
    meta = [f"Press meet: {chunk.press_meet_title or chunk.press_meet_id}"]
    if chunk.date:
        meta.append(f"Date: {chunk.date}")
    if chunk.speaker:
        meta.append(f"Speaker: {chunk.speaker}")
    if chunk.publication:
        meta.append(f"Publication: {chunk.publication}")
    meta.append(f"Language: {chunk.language}")
    return "\n".join(meta) + "\n\nPassage:\n" + chunk.text


def _call_once(llm, chunk: Chunk) -> Optional[dict]:
    system = _SYSTEM.format(types=onto.type_menu(), relations=onto.relation_menu())
    out = llm.complete_json(
        system, _user_prompt(chunk), onto.extraction_schema(),
        name="record_graph",
        description="Record the entities and relations found in the passage.",
        max_tokens=MAX_OUTPUT_TOKENS,
    )
    if out is None:
        return None
    return {"entities": out.get("entities") or [],
            "relations": out.get("relations") or []}


def extract_chunk(llm, chunk: Chunk, *, halve_on_empty: bool = True) -> Optional[dict]:
    """One chunk → {entities, relations}, or None if every attempt failed.

    The dominant failure here is not an error: `sarvam-105b` spends 2500-4000
    tokens reasoning, the subscription tier caps output at 4096, and a passage
    that needs more than the remainder comes back as an empty message with
    `finish_reason=length`. Nothing raises, so the provider's retry loop never
    engages — the call is simply billed for nothing.

    Retrying that verbatim mostly reproduces it, since the reasoning length
    tracks the input. Halving the input is what actually changes the outcome, so
    on an empty reply the chunk is split and the pieces merged. Measured failure
    without this was ~32% of the corpus.
    """
    out = _call_once(llm, chunk)
    if out is not None or not halve_on_empty:
        return out

    pieces = _halve(chunk)
    if len(pieces) < 2:
        return None
    parts = [p for p in (_call_once(llm, piece) for piece in pieces) if p is not None]
    return _merge_records(parts) if parts else None


def _halve(c: Chunk) -> list[Chunk]:
    """Split a chunk into exactly two pieces at the word boundary nearest its
    middle.

    Exactly two, not "as many as fit under a limit": the retry then costs a
    bounded two extra calls rather than a tail of ever-smaller fragments, and
    each half is small enough that the model's reasoning fits the output cap.
    """
    import hashlib

    text = c.text
    if len(text) < 800:
        return [c]
    mid = len(text) // 2
    cut = text.rfind(" ", mid - 200, mid + 200)
    if cut <= 0:
        cut = mid
    out: list[Chunk] = []
    for i, body in enumerate((text[:cut].strip(), text[cut:].strip())):
        if not body:
            continue
        p = c.model_copy()
        p.text = body
        p.text_original = body
        p.chunk_id = f"{c.chunk_id}#h{i}"
        p.content_hash = hashlib.sha1(
            " ".join(body.split()).encode("utf-8")).hexdigest()
        out.append(p)
    return out if len(out) == 2 else [c]


def validate(payload: dict, issues: Counter) -> dict:
    """Drop anything the ontology does not allow, counting each rejection.

    The enums in the tool schema already constrain the model, but they cannot
    express which *pairings* are legal, and a schema is a request rather than a
    guarantee. Rejections are tallied rather than silently dropped: every defect
    this project has had to dig out afterwards was one a silent skip had hidden.
    """
    ents: list[dict] = []
    by_name: dict[str, dict] = {}
    for e in payload.get("entities") or []:
        # The model sometimes returns a bare string where the schema asks for an
        # object — `["YS Jagan", "TTD"]` instead of `[{"name": …, "type": …}]`.
        # An entity with no type cannot be placed in the graph, so it is counted
        # and dropped; before this guard it raised AttributeError inside a worker
        # and cost the whole unit.
        if not isinstance(e, dict):
            issues["bad_entity"] += 1
            issues["untyped_entity_string"] += 1
            continue
        name = (e.get("name") or "").strip()
        etype = (e.get("type") or "").strip()
        if not name or not onto.is_valid_type(etype) or etype in onto.STRUCTURAL_TYPES:
            issues["bad_entity"] += 1
            continue
        rec = {"name": name, "type": etype,
               "english_name": (e.get("english_name") or "").strip()}
        ents.append(rec)
        by_name.setdefault(onto.normalise(name), rec)

    rels: list[dict] = []
    for r in payload.get("relations") or []:
        if not isinstance(r, dict):          # same defect on the relation side
            issues["unresolved_endpoint"] += 1
            continue
        src = by_name.get(onto.normalise(r.get("source") or ""))
        dst = by_name.get(onto.normalise(r.get("target") or ""))
        rel = (r.get("relation") or "").strip()
        if src is None or dst is None:
            issues["unresolved_endpoint"] += 1
            continue
        if not onto.is_valid_triple(rel, src["type"], dst["type"]):
            issues["invalid_triple"] += 1
            continue
        rels.append({"source": src["name"], "relation": rel, "target": dst["name"],
                     "evidence": (r.get("evidence") or "").strip()[:400]})
    return {"entities": ents, "relations": rels}


def _merge_records(parts: list[dict]) -> dict:
    """Combine several sub-piece extractions into one record for the parent chunk.

    Indexes defensively. This merges **raw model output** — `validate` runs later,
    on the merged result — so a record whose entity is missing `type`, or whose
    relation is missing an endpoint, arrives here intact. Subscripting it raised
    `KeyError: 'type'` inside a worker, which `fut.result()` re-raised on the main
    thread and killed an entire multi-hour pass on one malformed reply out of
    thousands. A dropped fragment costs one entity; an exception costs the run.
    """
    ents: dict[tuple[str, str], dict] = {}
    rels: dict[tuple[str, str, str], dict] = {}
    for p in parts:
        if not isinstance(p, dict):
            continue
        for e in p.get("entities") or []:
            if not isinstance(e, dict):
                continue
            etype, name = e.get("type"), e.get("name")
            if not etype or not name:
                continue
            ents.setdefault((str(etype), onto.normalise(str(name))), e)
        for r in p.get("relations") or []:
            if not isinstance(r, dict):
                continue
            src, rel, dst = r.get("source"), r.get("relation"), r.get("target")
            if not src or not rel or not dst:
                continue
            rels.setdefault(
                (onto.normalise(str(src)), str(rel), onto.normalise(str(dst))), r)
    return {"entities": list(ents.values()), "relations": list(rels.values())}


def _extract_all(llm, by_file: dict[str, list[Chunk]], memo: dict[str, dict],
                 memo_lock, issues: Counter, concurrency: int, fingerprint: str,
                 man=None, commit_every: int = 5,
                 retry_split: int = 0) -> tuple[dict[str, dict], int]:
    """Extract every outstanding chunk across all files from one flat queue.

    Pooling *within* a file and walking files sequentially wastes almost all the
    concurrency on this corpus: 78 selected chunks are spread over 62 files, so
    the median file holds one chunk and N workers do the work of one. Measured
    on the SECI meet, raising concurrency from 6 to 16 that way moved throughput
    from ~3/min to ~4/min. One queue over every chunk is what actually scales.

    Results are committed per file every `commit_every` completions, so a kill
    loses at most that much paid work.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    caches: dict[str, dict] = {rel: load_cache(rel, fingerprint) for rel in by_file}
    todo: list[tuple[str, Chunk]] = []
    for rel, chunks in by_file.items():
        for c in chunks:
            key = c.content_hash or c.chunk_id
            if key in caches[rel]:
                continue
            hit = memo.get(key)
            if hit is not None:
                caches[rel][key] = hit             # duplicate passage, already paid
                issues["cache_reuse"] += 1
                continue
            todo.append((rel, c))

    if not todo:
        return caches, 0

    def work(item: tuple[str, Chunk]):
        rel, c = item
        key = c.content_hash or c.chunk_id
        if retry_split <= 0 or len(c.text) <= retry_split:
            raw = extract_chunk(llm, c)
            return rel, key, (validate(raw, issues) if raw is not None else None)
        # Retry pass: this chunk already failed once at the normal size, almost
        # always because the model spent the whole output cap reasoning. Give it
        # less to chew on, then merge the pieces back under the *parent* hash so
        # the retry is idempotent and a later run does not re-bill it.
        parts = []
        for piece in _split_for_extraction(c, retry_split):
            raw = extract_chunk(llm, piece)
            if raw is not None:
                parts.append(validate(raw, issues))
        return rel, key, (_merge_records(parts) if parts else None)

    made = 0
    done = 0
    dirty: set[str] = set()
    total = len(todo)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(work, item) for item in todo]
        for fut in as_completed(futures):
            try:
                rel, key, result = fut.result()
            except Exception as e:
                # A worker exception used to propagate here and abort the whole
                # pass — hours of paid work lost to one malformed reply. Count it
                # and keep going; the chunk stays uncached, so a later run or
                # `--retry-split` picks it up.
                issues["worker_error"] += 1
                issues[f"worker_error:{type(e).__name__}"] += 1
                done += 1
                print(f"  worker error ({type(e).__name__}: {e}) — continuing",
                      file=sys.stderr, flush=True)
                continue
            done += 1
            if result is None:
                issues["extract_failed"] += 1
            else:
                caches[rel][key] = result
                with memo_lock:
                    memo[key] = result
                dirty.add(rel)
                made += 1
            if done % commit_every == 0:
                for r in dirty:
                    save_cache(r, caches[r], fingerprint)
                dirty.clear()
                if man is not None:
                    man.save()
                import time as _t
                print(f"  [{done}/{total}] {made} extracted · "
                      f"{issues['extract_failed']} failed · "
                      f"{getattr(llm, 'truncated', 0)} salvaged · "
                      f"{_t.strftime('%H:%M:%S')}", file=sys.stderr)

    for r in dirty:
        save_cache(r, caches[r], fingerprint)
    return caches, made


# ── phase C: resolution ────────────────────────────────────────────────────────
class _Union:
    """Minimal union-find over normalised (type, name) keys."""

    def __init__(self) -> None:
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(self, k: tuple[str, str]) -> tuple[str, str]:
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _is_latin(name: str) -> bool:
    letters = [ch for ch in name if ch.isalpha()]
    return bool(letters) and sum(ch in _LATIN for ch in letters) / len(letters) > 0.6


def resolve_entities(observations: list[tuple[str, str, str]]) -> dict[tuple[str, str], str]:
    """Fold surface forms onto canonical names.

    `observations` is (type, name, english_name). Three signals are used, in
    order of how much they can be trusted:

      1. the ontology's hand-written alias table — authoritative, and the only
         thing that knows "జగన్" and "YSJ" are the same person;
      2. the `english_name` the extractor returned alongside a Telugu name —
         links the two scripts for entities the alias table never listed;
      3. exact match on the normalised form.

    Deliberately no fuzzy string merging by default. In a system whose output is
    citations, wrongly merging two politicians attributes one man's words to
    another, which is worse than leaving a duplicate node that a later alias
    entry can fix.

    Returns (type, normalised surface) -> canonical display name.
    """
    uf = _Union()
    counts: Counter = Counter()
    forms: dict[tuple[str, str], Counter] = defaultdict(Counter)

    for etype, name, english in observations:
        key = (etype, onto.normalise(name))
        canon_key = (etype, onto.normalise(onto.canonical_name(name)))
        uf.union(canon_key, key)
        counts[key] += 1
        forms[canon_key][name] += 1
        if english and onto.normalise(english) != onto.normalise(name):
            ekey = (etype, onto.normalise(english))
            uf.union(canon_key, ekey)
            forms[canon_key][english] += 1
            # Also fold the English form through the alias table, so a Telugu
            # name whose English spelling *is* a known alias reaches the canon.
            uf.union((etype, onto.normalise(onto.canonical_name(english))), ekey)

    groups: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for key in list(uf.parent):
        groups[uf.find(key)].append(key)

    display: dict[tuple[str, str], str] = {}
    for root, members in groups.items():
        etype = root[0]
        pool: Counter = Counter()
        for m in members:
            pool.update(forms.get(m, {}))
        if not pool:
            continue
        # Prefer the ontology's canonical spelling; then a Latin-script form, so
        # citations read consistently; then whichever spelling is most common.
        known = [n for n in pool if onto.normalise(onto.canonical_name(n)) != onto.normalise(n)]
        if known:
            best = onto.canonical_name(known[0])
        else:
            latin = {n: c for n, c in pool.items() if _is_latin(n)}
            best = max((latin or pool).items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
        for m in members:
            display[m] = best
    return display


# ── phase E: community summaries ───────────────────────────────────────────────
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string",
                  "description": "Six words or fewer naming this cluster's theme."},
        "summary": {"type": "string",
                    "description": "3-5 sentences on what connects these entities "
                                   "and what the recurring dispute or topic is."},
    },
    "required": ["title", "summary"],
}

_SUMMARY_SYSTEM = (
    "You summarise a cluster from a knowledge graph built out of Andhra Pradesh "
    "political press meets. You are given the cluster's entities and the relations "
    "between them. Describe only what those facts support — do not add background "
    "knowledge, and do not speculate. Write in English."
)


def _community_brief(G, community, max_entities: int = 40,
                     max_edges: int = 60) -> str:
    members = sorted(community.entity_ids,
                     key=lambda n: -G.nodes.get(n, {}).get("mention_count", 0))
    lines = ["Entities:"]
    for nid in members[:max_entities]:
        d = G.nodes.get(nid, {})
        lines.append(f"- {d.get('name', nid)} [{d.get('type', '')}] "
                     f"({d.get('mention_count', 0)} mentions)")
    inside = set(community.entity_ids)
    edges = [(u, v, k, d) for u, v, k, d in G.edges(keys=True, data=True)
             if u in inside and v in inside and not d.get("structural")]
    edges.sort(key=lambda e: -e[3].get("weight", 1))
    if edges:
        lines.append("\nRelations:")
        for u, v, k, d in edges[:max_edges]:
            lines.append(f"- {G.nodes[u].get('name', u)} --{k}--> "
                         f"{G.nodes[v].get('name', v)} (×{d.get('weight', 1)})")
    if community.press_meet_ids:
        lines.append("\nPress meets: " + ", ".join(sorted(community.press_meet_ids)[:20]))
    return "\n".join(lines)


def summarise_communities(G, communities: list, llm, *, min_size: int = 3,
                          concurrency: int = 4) -> int:
    """Fill in each community's title and summary. One paid call apiece.

    Singletons and pairs are skipped: their "theme" is just their members, and at
    a few hundred communities the calls would cost more than they explain.
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [c for c in communities if c.size >= min_size]

    def work(c):
        out = llm.complete_json(
            _SUMMARY_SYSTEM, _community_brief(G, c), _SUMMARY_SCHEMA,
            name="record_community",
            description="Record this cluster's theme.", max_tokens=600)
        return c, out

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for c, out in pool.map(work, targets):
            if not out:
                continue
            c.title = (out.get("title") or "").strip()
            c.summary = (out.get("summary") or "").strip()
            done += 1
    return done


# ── orchestration ──────────────────────────────────────────────────────────────
def run_extraction_phases(G, man, files: list[Path],
                          chunks_by_file: dict[str, list[Chunk]], counts: dict,
                          *, force: bool = False, concurrency: int | None = None,
                          summaries: bool = True, retry_split: int = 0) -> None:
    """Phases B and C, layering LLM entities/relations onto the structural graph.

    Mutates `G` and `counts` in place. Phase E runs from `graph_build` once
    communities exist.
    """
    from ..providers import get_llm

    s = settings()
    workers = concurrency or s.llm_concurrency
    llm = get_llm()
    issues: Counter = Counter()
    fingerprint = cache_fingerprint(llm)
    memo = load_all_cached(files, fingerprint)
    memo_lock = threading.Lock()

    selected, skipped = select_chunks(chunks_by_file)
    by_file: dict[str, list[Chunk]] = defaultdict(list)
    for rel, c in selected:
        by_file[rel].append(c)
    counts["chunks_selected"] = len(selected)
    counts["chunks_skipped"] = {k: v for k, v in skipped.items()
                                if not k.startswith("file:")}
    print(f"graph[B] extraction: {len(selected)}/{sum(len(v) for v in chunks_by_file.values())} "
          f"chunks selected · {len(memo)} already cached · {workers} workers · "
          f"provider {type(llm).__name__}", file=sys.stderr)
    print(f"  skipped: {counts['chunks_skipped']}", file=sys.stderr)

    # ── B: per-chunk extraction, gated by the cache, not the manifest ──
    # The extraction cache is the only honest record of what has been paid for,
    # so it is what decides the work. The manifest's per-file `graph` mark is
    # bookkeeping that rides alongside: the two can desync (a cleared cache, a
    # fixture run marking files done) and when they do, trusting the manifest
    # silently skips extraction and reports "0 new calls" as if it had succeeded.
    for p in files:
        man.needs(str(p.relative_to(ROOT)), p, "graph")   # register/refresh entry

    outstanding = sum(1 for _, c in selected
                      if (c.content_hash or c.chunk_id) not in memo)
    counts["skipped"] = len(selected) - outstanding
    if not outstanding and not force:
        print("  every selected chunk is already extracted — nothing to bill",
              file=sys.stderr)

    per_file, new_calls = _extract_all(llm, by_file, memo, memo_lock, issues,
                                       workers, fingerprint, man=man,
                                       retry_split=retry_split)
    for rel in by_file:
        man.mark(rel, "graph", "done")
    man.save()

    # Files with no outstanding work still have to contribute their cached
    # results, or a resumed run would rebuild the graph from only its own tail.
    for path in files:
        rel = str(path.relative_to(ROOT))
        if rel not in per_file and rel in chunks_by_file:
            cached = load_cache(rel, fingerprint)
            if cached:
                per_file[rel] = cached

    counts["llm_calls"] = llm.calls
    # Cumulative, so a resumed run reports the whole paid pass rather than only
    # its own tail — the same reason `_describe_graph` exists.
    counts["extractions_cached"] = sum(len(c) for c in per_file.values())
    counts["sarvam_calls"] = dict(counts.get("sarvam_calls") or {})
    counts["sarvam_calls"]["graph"] = new_calls
    for k in ("extract_failed", "invalid_triple", "unresolved_endpoint",
              "bad_entity", "cache_reuse"):
        if issues[k]:
            counts["sarvam_calls"][k] = issues[k]
    print(f"graph[B] extraction: {new_calls} new calls · {llm.retries} retries · "
          f"{issues['extract_failed']} failed · {issues['cache_reuse']} duplicates reused",
          file=sys.stderr)
    if issues["extract_failed"]:
        # Without this the stage reports a failure count and no reason, which is
        # how a deprecated-model 400 looked identical to a model that simply
        # found nothing. The provider's last error is the fastest way in.
        counts["last_error"] = getattr(llm, "last_error", "")
        print(f"  last provider error: {counts['last_error'][:300]}", file=sys.stderr)

    # ── C: resolution, then load into the graph ──
    observations: list[tuple[str, str, str]] = []
    for rel, cache in per_file.items():
        for payload in cache.values():
            for e in payload.get("entities") or []:
                observations.append((e["type"], e["name"], e.get("english_name", "")))
    display = resolve_entities(observations)
    counts["entity_observations"] = len(observations)
    counts["resolved_names"] = len(display)
    print(f"graph[C] resolution: {len(observations)} mentions → "
          f"{len(set(display.values()))} canonical names", file=sys.stderr)

    _load_into_graph(G, per_file, chunks_by_file, display, counts)


def _canonical(display: dict, etype: str, name: str) -> str:
    return display.get((etype, onto.normalise(name))) or onto.canonical_name(name)


def _load_into_graph(G, per_file: dict[str, dict[str, dict]],
                     chunks_by_file: dict[str, list[Chunk]],
                     display: dict, counts: dict) -> None:
    """Attach resolved entities and relations to the structural scaffold.

    Every entity is also joined to its press meet with MENTIONS, which is what
    makes "which meets discussed the laddu row" answerable and gives each edge a
    dated, citable anchor.
    """
    min_mentions = settings().min_entity_mentions
    mention_totals: Counter = Counter()
    pending_rels: list[Relation] = []
    added_entities = 0

    for rel, cache in per_file.items():
        for chunk in chunks_by_file.get(rel) or []:
            payload = cache.get(chunk.content_hash or chunk.chunk_id)
            if not payload:
                continue
            meet_id = chunk.press_meet_id or "unknown"
            meet_node = onto.entity_id("PressMeet", meet_id)
            local: dict[str, tuple[str, str]] = {}       # surface -> (id, type)

            for e in payload.get("entities") or []:
                etype, surface = e["type"], e["name"]
                canon = _canonical(display, etype, surface)
                nid = onto.entity_id(etype, canon)
                local[onto.normalise(surface)] = (nid, etype)
                mention_totals[nid] += 1
                gstore.add_entity(G, Entity(
                    entity_id=nid, name=canon, type=etype,
                    aliases=[surface] if onto.normalise(surface) != onto.normalise(canon) else [],
                    mention_count=1, press_meet_ids=[meet_id],
                    first_date=chunk.date, last_date=chunk.date))
                added_entities += 1
                if onto.is_valid_triple("MENTIONS", "PressMeet", etype):
                    pending_rels.append(Relation(
                        source_id=meet_node, relation="MENTIONS", target_id=nid,
                        chunk_id=chunk.chunk_id, source_file=rel,
                        press_meet_id=meet_id, date=chunk.date,
                        citation=chunk.citation))
                if etype == "Issue":
                    pending_rels.append(Relation(
                        source_id=meet_node, relation="ABOUT_ISSUE", target_id=nid,
                        chunk_id=chunk.chunk_id, source_file=rel,
                        press_meet_id=meet_id, date=chunk.date,
                        citation=chunk.citation))
                    src_node = onto.entity_id("Source", rel)
                    if src_node in G:
                        pending_rels.append(Relation(
                            source_id=src_node, relation="RELATED_TO_ISSUE",
                            target_id=nid, chunk_id=chunk.chunk_id, source_file=rel,
                            press_meet_id=meet_id, date=chunk.date,
                            citation=chunk.citation))

            for r in payload.get("relations") or []:
                src = local.get(onto.normalise(r["source"]))
                dst = local.get(onto.normalise(r["target"]))
                if src is None or dst is None:
                    continue
                pending_rels.append(Relation(
                    source_id=src[0], relation=r["relation"], target_id=dst[0],
                    evidence=r.get("evidence", ""), chunk_id=chunk.chunk_id,
                    source_file=rel, press_meet_id=meet_id, date=chunk.date,
                    citation=chunk.citation))

    dropped = 0
    if min_mentions > 1:
        weak = {nid for nid, n in mention_totals.items() if n < min_mentions}
        for nid in weak:
            if nid in G:
                G.remove_node(nid)
        dropped = len(weak)

    for r in pending_rels:
        gstore.add_relation(G, r)

    counts["entity_mentions"] = added_entities
    counts["entities_dropped"] = dropped
    counts["relations_asserted"] = len(pending_rels)
    print(f"graph[C] loaded: {added_entities} mentions · {len(pending_rels)} relation "
          f"assertions · {dropped} entities below the mention floor", file=sys.stderr)
