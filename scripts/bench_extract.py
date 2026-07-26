#!/usr/bin/env python
"""Benchmark extraction configs on real chunks before committing a long paid pass.

    .venv/bin/python scripts/bench_extract.py                 # full comparison
    .venv/bin/python scripts/bench_extract.py --fresh 6 --base 4
    .venv/bin/python scripts/bench_extract.py --probe-only    # 1 call, cap check

The graph pass costs hours and money, and the two knobs that could shorten it —
a smaller model, less reasoning — trade quality for speed in ways only real
passages reveal. This measures both sides on the same sample: wall-clock and
calls per unit, and how much of the 105b baseline's entity set a cheaper config
still finds.

**Each config runs in its own subprocess.** `Settings` evaluates its `_env(...)`
defaults in the class body, so every setting is frozen when `kng.config` is
imported; changing `os.environ` afterwards does nothing. Passing the environment
to a fresh interpreter is the only faithful way to switch model or reasoning
effort — and it is what production does too.

Writes nothing under `index/`: `extract_chunk` is pure, and only
`run_extraction` persists. The fresh units it extracts are therefore paid for and
discarded, which is the price of measuring before committing.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Scope every config to the same speech-first selection the real pass will use,
# so the sample is drawn from the passages that actually matter.
SCOPE = "press_release,news_clip,video,slide"

CONFIGS = [
    ("105b/low", {"SARVAM_CHAT_MODEL": "sarvam-105b", "LLM_REASONING_EFFORT": "low"}),
    ("30b/low", {"SARVAM_CHAT_MODEL": "sarvam-30b", "LLM_REASONING_EFFORT": "low"}),
    ("30b/null", {"SARVAM_CHAT_MODEL": "sarvam-30b", "LLM_REASONING_EFFORT": "null"}),
    ("105b/null", {"SARVAM_CHAT_MODEL": "sarvam-105b", "LLM_REASONING_EFFORT": "null"}),
]


# ── sample ─────────────────────────────────────────────────────────────────────
def _load_units():
    """(fresh, baseline) units: never-extracted, and already-cached with records.

    Deterministic: `select_chunks` walks files in sorted order, so both lists are
    identical across configs and the comparison is like-for-like.
    """
    import json as _json

    from kng.models import Chunk
    from kng.pipeline import graph_extract as gx

    chunks_by_file: dict[str, list[Chunk]] = {}
    for p in sorted((ROOT / "index" / "chunks").rglob("*.json")):
        recs = _json.loads(p.read_text(encoding="utf-8"))
        if isinstance(recs, list) and recs:
            cs = [Chunk(**r) for r in recs]
            chunks_by_file[cs[0].source_file] = cs

    selected, _ = gx.select_chunks(chunks_by_file)
    fresh, baseline = [], []
    caches: dict[str, dict] = {}
    for rel, c in selected:
        if rel not in caches:
            caches[rel] = gx.load_cache(rel)          # every record, any fingerprint
        rec = caches[rel].get(c.content_hash or c.chunk_id)
        if rec is None:
            fresh.append(c)
        elif rec.get("entities"):
            baseline.append((c, rec))
    return fresh, baseline


def _names(record) -> set[str]:
    from kng.graph import ontology as onto
    return {onto.normalise(e.get("name", "")) for e in (record or {}).get("entities") or []
            if e.get("name")}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── worker: one config, in a fresh interpreter ─────────────────────────────────
def run_worker(n_fresh: int, n_base: int, concurrency: int) -> dict:
    from kng.pipeline import graph_extract as gx
    from kng.providers import get_llm

    llm = get_llm()
    fresh_all, base_all = _load_units()
    # Spread the sample across the corpus rather than taking the first N, which
    # would measure one press meet's writing style.
    fresh = fresh_all[:: max(1, len(fresh_all) // max(1, n_fresh))][:n_fresh]
    base = base_all[:: max(1, len(base_all) // max(1, n_base))][:n_base]

    out: dict = {"model": getattr(llm, "model", "?"),
                 "effort": os.environ.get("LLM_REASONING_EFFORT", ""),
                 "fresh_units": len(fresh), "base_units": len(base)}

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda c: gx.extract_chunk(llm, c), fresh))
    elapsed = time.monotonic() - t0

    ok = [r for r in results if r]
    out.update({
        "wall_s": round(elapsed, 1),
        "s_per_unit": round(elapsed / max(1, len(fresh)), 1),
        "units_per_min": round(60 * len(fresh) / max(0.1, elapsed), 2),
        "calls": llm.calls, "calls_per_unit": round(llm.calls / max(1, len(fresh)), 2),
        "retries": llm.retries, "failures": llm.failures,
        "truncated": llm.truncated,
        "empty_results": len(results) - len(ok),
        "entities_per_unit": round(sum(len(r.get("entities") or []) for r in ok)
                                   / max(1, len(fresh)), 1),
        "relations_per_unit": round(sum(len(r.get("relations") or []) for r in ok)
                                    / max(1, len(fresh)), 1),
    })

    # Quality: re-extract passages whose 105b record is already cached and compare.
    overlaps, yields = [], []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        again = list(pool.map(lambda cb: gx.extract_chunk(llm, cb[0]), base))
    for (c, cached), fresh_rec in zip(base, again):
        overlaps.append(_jaccard(_names(fresh_rec), _names(cached)))
        yields.append(len(_names(fresh_rec)) / max(1, len(_names(cached))))
    if overlaps:
        out["entity_overlap"] = round(sum(overlaps) / len(overlaps), 3)
        out["yield_vs_baseline"] = round(sum(yields) / len(yields), 2)
    return out


def probe_max_tokens(value: int = 8192) -> dict:
    """Does this tier accept max_tokens above the 4096 WP3 measured?

    Decides whether several chunks can share one call — the only remaining way to
    cut request count once the model and reasoning effort are settled.
    """
    from kng.config import settings
    from kng.providers.sarvam import chat_completion

    s = settings()
    payload = {"model": s.sarvam_chat_model, "max_tokens": value,
               "reasoning_effort": "low", "temperature": 0.0,
               "messages": [{"role": "user",
                             "content": "Count from 1 to 400, one number per line."}]}
    t0 = time.monotonic()
    try:
        r = chat_completion(payload)
        usage = r.get("usage") or {}
        return {"max_tokens_requested": value, "accepted": True,
                "completion_tokens": usage.get("completion_tokens"),
                "finish_reason": (r.get("choices") or [{}])[0].get("finish_reason"),
                "elapsed_s": round(time.monotonic() - t0, 1)}
    except Exception as e:
        return {"max_tokens_requested": value, "accepted": False,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "elapsed_s": round(time.monotonic() - t0, 1)}


# ── parent: run every config, print the table ──────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(prog="bench_extract")
    ap.add_argument("--fresh", type=int, default=8, help="units timed per config")
    ap.add_argument("--base", type=int, default=5, help="cached units re-extracted for quality")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--worker", help=argparse.SUPPRESS)     # internal
    ap.add_argument("--probe-worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.probe_worker:
        print("RESULT " + json.dumps(probe_max_tokens()))
        return 0
    if args.worker:
        print("RESULT " + json.dumps(
            run_worker(args.fresh, args.base, args.concurrency)))
        return 0

    def spawn(extra_env: dict, probe: bool = False) -> dict:
        env = {**os.environ, "GRAPH_SOURCE_TYPES": SCOPE, **extra_env}
        cmd = [sys.executable, __file__, "--fresh", str(args.fresh),
               "--base", str(args.base), "--concurrency", str(args.concurrency)]
        cmd += ["--probe-worker"] if probe else ["--worker", "1"]
        p = subprocess.run(cmd, env=env, capture_output=True, text=True)
        for line in reversed(p.stdout.splitlines()):
            if line.startswith("RESULT "):
                return json.loads(line[len("RESULT "):])
        return {"error": (p.stderr or p.stdout or "no output")[-300:]}

    print("probing the output-token ceiling …", flush=True)
    probe = spawn({"SARVAM_CHAT_MODEL": "sarvam-105b"}, probe=True)
    print(json.dumps(probe, indent=1))
    if args.probe_only:
        return 0

    rows = []
    for name, env in CONFIGS:
        print(f"\nrunning {name} …", flush=True)
        row = spawn(env)
        row["config"] = name
        rows.append(row)
        print(json.dumps(row, indent=1), flush=True)

    cols = ["config", "units_per_min", "s_per_unit", "calls_per_unit", "truncated",
            "empty_results", "failures", "entities_per_unit", "relations_per_unit",
            "entity_overlap", "yield_vs_baseline"]
    print("\n" + " | ".join(f"{c:>17}" for c in cols))
    print("-" * (20 * len(cols)))
    for r in rows:
        print(" | ".join(f"{str(r.get(c, '-')):>17}" for c in cols))
    print("\nDecision rule: fastest config whose entity_overlap >= ~0.8 and "
          "yield_vs_baseline is not materially below 1.0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
