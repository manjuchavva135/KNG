"""The graph schema — first and only reader of `config/ontology.yaml`.

Two jobs, and it matters that one file does both:

  * **constrain** the LLM extractor — `extraction_schema()` turns the ontology
    into a JSON schema whose enums are the legal node and relation types, so the
    model cannot invent a type;
  * **validate** what comes back — `is_valid_triple()` re-checks every edge
    against the same `from`/`to` declarations before it reaches the graph.

Prompt and validator therefore cannot drift apart. Illegal triples are rejected
and *counted* by the caller rather than dropped quietly: every defect this
project has had to dig out afterwards (base64 in the corpus, 512-token
truncation, cleanup failing on all 79 office docs) was one a bare `except` or a
silent skip had hidden.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any, Iterable

from ..config import ROOT

ONTOLOGY_PATH = ROOT / "config" / "ontology.yaml"

# Dropped from the head of a name before matching. Telugu honorifics are written
# as separate words, so a plain prefix strip handles both scripts.
_HONORIFICS = (
    "sri", "shri", "smt", "smt.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
    "dr", "dr.", "shree", "sree", "శ్రీ", "శ్రీమతి", "డా",
)

# Punctuation that carries no identity: "Y.S. Jagan" and "YS Jagan" are the same
# person, and "Kutami/NDA" should not fragment on the slash.
_PUNCT = re.compile(r"[.·,;:!?'\"()\[\]{}<>/\\|_*`~‘’“”-]+")
_WS = re.compile(r"\s+")


def _load_raw() -> dict[str, Any]:
    import yaml
    if not ONTOLOGY_PATH.exists():
        raise FileNotFoundError(f"ontology not found: {ONTOLOGY_PATH}")
    return yaml.safe_load(ONTOLOGY_PATH.read_text(encoding="utf-8")) or {}


# ── names ──────────────────────────────────────────────────────────────────────
def normalise(name: str) -> str:
    """Fold a surface name to its match key.

    NFKC first because the corpus mixes scripts and Telugu arrives in several
    normalisation forms from OCR vs ASR vs local parse; the same word must fold
    to one key regardless of which extractor produced it.
    """
    s = unicodedata.normalize("NFKC", (name or "")).strip().casefold()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    parts = s.split(" ")
    while parts and parts[0] in _HONORIFICS:
        parts.pop(0)
    return " ".join(parts)


@lru_cache(maxsize=1)
def node_types() -> dict[str, str]:
    """Legal node type -> its description (the description feeds the prompt)."""
    raw = _load_raw().get("node_types") or {}
    return {k: (v or {}).get("desc", "") for k, v in raw.items()}


@lru_cache(maxsize=1)
def relation_types() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Legal relation type -> (allowed source types, allowed target types).

    `from`/`to` may each be a scalar or a list in the YAML; both collapse to a
    frozenset here so callers never branch on it.
    """
    def _as_set(v: Any) -> frozenset[str]:
        if v is None:
            return frozenset()
        return frozenset([v] if isinstance(v, str) else list(v))

    out: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for name, spec in (_load_raw().get("relationship_types") or {}).items():
        spec = spec or {}
        out[name] = (_as_set(spec.get("from")), _as_set(spec.get("to")))
    return out


@lru_cache(maxsize=1)
def alias_map() -> dict[str, str]:
    """Normalised variant -> canonical name, seeded from the ontology.

    The canonical form maps to itself too, so a single lookup resolves both
    "జగన్" and the already-canonical "Y. S. Jagan Mohan Reddy".
    """
    out: dict[str, str] = {}
    for canonical, variants in (_load_raw().get("aliases") or {}).items():
        out[normalise(canonical)] = canonical
        for v in variants or []:
            out[normalise(v)] = canonical
    return out


def canonical_name(name: str) -> str:
    """Alias-table lookup; returns `name` unchanged when it isn't a known alias."""
    return alias_map().get(normalise(name), name)


def entity_id(node_type: str, name: str) -> str:
    """Stable node id — derived, never assigned.

    Deriving it from (type, normalised canonical name) means a rebuild produces
    identical ids, and the same entity written "జగన్" in a clip and "YS Jagan"
    in a press release lands on one node. Typed, so the Place "Amaravati" and a
    hypothetical Scheme of the same name stay distinct.
    """
    return f"{node_type}:{normalise(canonical_name(name))}"


# ── validation ─────────────────────────────────────────────────────────────────
def is_valid_type(node_type: str) -> bool:
    return node_type in node_types()


def is_valid_triple(rel: str, src_type: str, dst_type: str) -> bool:
    """True when `rel` is declared and both endpoint types are allowed for it."""
    spec = relation_types().get(rel)
    if spec is None:
        return False
    src_ok, dst_ok = spec
    return src_type in src_ok and dst_type in dst_ok


def relations_between(src_type: str, dst_type: str) -> list[str]:
    """Every relation legal between two node types — used to steer the prompt."""
    return sorted(r for r, (s, d) in relation_types().items()
                  if src_type in s and dst_type in d)


# ── prompt / schema generation ─────────────────────────────────────────────────
# Types the LLM must not invent from free text: PressMeet, Source, Publication
# and Date come from file metadata, which is authoritative and free. Letting the
# model emit them too would create duplicate, unciteable meet nodes.
STRUCTURAL_TYPES = frozenset({"PressMeet", "Source", "Publication", "Date"})


def extractable_types() -> list[str]:
    return sorted(t for t in node_types() if t not in STRUCTURAL_TYPES)


def extractable_relations() -> list[str]:
    """Relations whose endpoints the extractor is allowed to produce."""
    types = set(extractable_types())
    return sorted(r for r, (s, d) in relation_types().items()
                  if (s & types) and (d & types))


def type_menu() -> str:
    """The node-type block of the extraction prompt, straight from the ontology."""
    descs = node_types()
    return "\n".join(f"- {t}: {descs[t]}" for t in extractable_types())


def relation_menu() -> str:
    """The relation block, rendered with its endpoint constraints."""
    rels = relation_types()
    lines = []
    for r in extractable_relations():
        src, dst = rels[r]
        lines.append(f"- {r}: {'|'.join(sorted(src))} -> {'|'.join(sorted(dst))}")
    return "\n".join(lines)


def extraction_schema() -> dict[str, Any]:
    """JSON schema for the forced tool call, with enums taken from the ontology.

    Constraining `type` and `relation` to enums here is what stops the model
    inventing categories; `is_valid_triple` then still re-checks the pairing,
    which the enums alone cannot express.
    """
    return {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "description": "Every distinct entity named in the passage.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Name as written in the passage."},
                        "type": {"type": "string", "enum": extractable_types()},
                        "english_name": {
                            "type": "string",
                            "description": "Common English/Latin-script form, if "
                                           "the name is written in Telugu or Hindi.",
                        },
                    },
                    "required": ["name", "type"],
                },
            },
            "relations": {
                "type": "array",
                "description": "Relations explicitly supported by the passage.",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string",
                                   "description": "`name` of an entity listed above."},
                        "relation": {"type": "string", "enum": extractable_relations()},
                        "target": {"type": "string",
                                   "description": "`name` of an entity listed above."},
                        "evidence": {
                            "type": "string",
                            "description": "Short quote from the passage supporting "
                                           "this relation.",
                        },
                    },
                    "required": ["source", "relation", "target"],
                },
            },
        },
        "required": ["entities", "relations"],
    }


def summary() -> dict[str, Any]:
    """Small dict for stats/handover reporting."""
    return {
        "node_types": len(node_types()),
        "relation_types": len(relation_types()),
        "extractable_types": len(extractable_types()),
        "extractable_relations": len(extractable_relations()),
        "alias_variants": len(alias_map()),
    }


def _iter_missing(names: Iterable[str]) -> list[str]:
    """Names that are not declared node types — for validating hand edits."""
    return sorted({n for n in names if n not in node_types()})


def main(argv: list[str] | None = None) -> int:
    """`python -m kng.graph.ontology` — inspect the loaded schema. Free."""
    import json
    print(json.dumps(summary(), indent=2))
    print("\nextractable node types:")
    print(type_menu())
    print("\nextractable relations:")
    print(relation_menu())
    bad = _iter_missing(t for spec in relation_types().values() for s in spec for t in s)
    if bad:
        print(f"\nWARNING: relation endpoints reference undeclared node types: {bad}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
