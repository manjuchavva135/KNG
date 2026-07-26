"""Vector + keyword passage retrieval over the LanceDB chunk table.

Dense and lexical retrieval fail in opposite directions on this corpus. bge-m3
matches a paraphrase across languages but blurs a proper noun it has never seen;
BM25 nails "SECI" or "₹3,000 crore" and misses the same claim written in Telugu.
Fusing them is cheaper and more robust than tuning either one.

Fusion is **Reciprocal Rank Fusion**: each leg contributes `1/(RRF_K + rank)` to
a document's score. Rank-based, so it never has to reconcile a cosine distance
with a BM25 score — two quantities with no shared scale — and it degrades to a
plain single-leg ranking when the other leg returns nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from ..store import vector as vstore

# Standard RRF damping. Large enough that the top few ranks of one leg cannot
# alone outvote broad agreement between both legs.
RRF_K = 60

# Both legs are over-fetched relative to k: fusion and duplicate-collapsing both
# discard rows, and the archive holds byte-identical files whose chunks are
# genuinely duplicate passages.
OVERFETCH = 4

# FTS query sanitiser. The native (tantivy-free) index parses its input, so a
# raw question's punctuation — quotes, "?", ":" — is a syntax error rather than
# a search term.
_FTS_STRIP = re.compile(r"[^\w\sఀ-౿ऀ-ॿ]+", re.UNICODE)
_ATTRIBUTION = re.compile(
    r"\b(?:jagan|said|say|statement|statements|allege|alleged|allegations|"
    r"criticise|criticized|response|respond)\b|"
    r"(?:జగన్|అన్నారు|చెప్పారు|ఆరోపణ|విమర్శ)", re.I)
_DOCUMENTARY = re.compile(
    r"\b(?:court|supreme court|sit|cag|report|order|verdict|conclude|"
    r"concluded|document|tariff filing)\b|(?:కోర్టు|నివేదిక|తీర్పు)", re.I)
_QUERY_STOP = {
    "what", "which", "when", "where", "why", "how", "did", "does", "said",
    "say", "about", "the", "and", "with", "from", "into", "were", "was",
    "jagan", "ys", "are", "its", "his", "her", "their", "this", "that",
    "గురించి", "జగన్", "ఏమన్నారు", "ఏమిటి", "ఏమి", "చెప్పారు",
}


def _query_terms(text: str) -> set[str]:
    return {
        token.lower() for token in _FTS_STRIP.sub(" ", text).split()
        if len(token) > 2 and not token.isdigit() and token.lower() not in _QUERY_STOP
    }


def rerank(question: str, rows: list[dict]) -> list[dict]:
    """Archive-aware second-stage ordering over the fused candidate pool.

    Most questions ask what Jagan said, while 59% of indexed chunks are
    third-party `source_doc` pages. Plain RRF therefore lets generic newspaper
    PDFs crowd out the press release carrying the attributable statement.
    Documentary questions (court/SIT/CAG/report) need the inverse preference.

    This is deliberately small and inspectable, not a pretend cross-encoder:
    the original fusion score, source prior and lexical coverage are retained on
    each row. A configured learned reranker can replace it later without hiding
    why today's ordering changed.
    """
    attribution = bool(_ATTRIBUTION.search(question or ""))
    documentary = bool(_DOCUMENTARY.search(question or ""))
    if documentary and not attribution:
        priors = {
            "source_doc": 1.06, "press_release": 1.00, "news_clip": 1.00,
            "video": 1.00, "slide": 1.00, "table": 0.96,
        }
        intent = "documentary"
    elif attribution:
        priors = {
            "press_release": 1.08, "video": 1.06, "news_clip": 1.04,
            "slide": 1.03, "source_doc": 0.96, "table": 0.94,
        }
        intent = "attribution"
    else:
        priors = {
            "press_release": 1.03, "video": 1.03, "news_clip": 1.02,
            "slide": 1.02, "source_doc": 0.98, "table": 0.96,
        }
        intent = "general"

    qterms = _query_terms(question)
    out = []
    for row in rows:
        item = dict(row)
        base = float(item.get("score") or 0.0)
        text_terms = _query_terms(
            f"{item.get('text', '')} {item.get('citation', '')}")
        coverage = len(qterms & text_terms) / len(qterms) if qterms else 0.0
        prior = priors.get(str(item.get("source_type") or ""), 0.90)
        item["fusion_score"] = base
        item["source_prior"] = prior
        item["term_coverage"] = round(coverage, 4)
        item["rerank_intent"] = intent
        # Coverage is bounded below one RRF leg's contribution. It breaks close
        # RRF ties; it cannot drag a lexical-only tail hit over broad hybrid
        # agreement by itself.
        item["score"] = base * prior + (coverage * 0.0015)
        out.append(item)
    return sorted(out, key=lambda r: -r["score"])


def diversify(rows: list[dict], k: int, max_per_file: int = 2) -> list[dict]:
    """Prevent one long PDF from occupying the complete answer context."""
    out: list[dict] = []
    skipped: list[dict] = []
    per_file: dict[str, int] = {}
    for row in rows:
        source = str(row.get("source_file") or row.get("chunk_id") or "")
        if per_file.get(source, 0) >= max_per_file:
            skipped.append(row)
            continue
        out.append(row)
        per_file[source] = per_file.get(source, 0) + 1
        if len(out) >= k:
            return out
    # A narrow user filter can legitimately leave one document. Return k when
    # possible instead of interpreting diversity as a hard result-count cap.
    for row in skipped:
        out.append(row)
        if len(out) >= k:
            break
    return out


@dataclass(frozen=True)
class Filters:
    """SQL prefilters over the provenance columns.

    The point of storing provenance as real columns in WP2: "what did he say
    about this *after* the election" is a `date >=` prefilter, not something the
    ranker should be asked to infer.
    """
    language: Optional[str] = None
    source_type: Optional[str] = None
    press_meet_id: Optional[str] = None
    publication: Optional[str] = None
    since: Optional[str] = None
    until: Optional[str] = None

    def where(self) -> Optional[str]:
        clauses: list[str] = []
        for column, value in (("language", self.language),
                              ("source_type", self.source_type),
                              ("press_meet_id", self.press_meet_id),
                              ("publication", self.publication)):
            if value:
                clauses.append(f"{column} = {_lit(value)}")
        if self.since:
            clauses.append(f"date >= {_lit(self.since)}")
        if self.until:
            clauses.append(f"date <= {_lit(self.until)}")
        return " AND ".join(clauses) or None


def _lit(value: str) -> str:
    """SQL string literal with quotes doubled — press-meet titles contain them."""
    return "'" + str(value).replace("'", "''") + "'"


def vector_leg(table, question: str, k: int, where: Optional[str]) -> list[dict]:
    from ..providers import get_embedder
    qv = get_embedder().embed_one(question, kind="query")
    return vstore.search(table, qv, k=k, where=where)


def keyword_leg(table, question: str, k: int, where: Optional[str]) -> list[dict]:
    """BM25 leg. Raises when the table has no FTS index — the caller reports it."""
    terms = _FTS_STRIP.sub(" ", question).strip()
    if not terms:
        return []
    q = table.search(terms, query_type="fts").limit(k)
    if where:
        # Postfilter, not prefilter: LanceDB's native FTS scores the matched set
        # first, and prefiltering an FTS query is not supported on every version.
        q = q.where(where)
    return q.to_list()


def fuse(legs: dict[str, list[dict]], weights: Optional[dict[str, float]] = None) -> list[dict]:
    """Reciprocal Rank Fusion over named legs, best first.

    Each surviving row keeps the per-leg rank that produced it, so a retrieval
    result can be explained ("keyword rank 1, vector rank 14") instead of only
    scored.
    """
    weights = weights or {}
    merged: dict[str, dict] = {}
    for leg, rows in legs.items():
        w = weights.get(leg, 1.0)
        for rank, row in enumerate(rows, start=1):
            key = row.get("chunk_id") or f"{leg}:{rank}"
            cur = merged.get(key)
            if cur is None:
                cur = dict(row)
                cur["score"] = 0.0
                cur["ranks"] = {}
                merged[key] = cur
            cur["score"] += w / (RRF_K + rank)
            cur["ranks"][leg] = rank
    out = sorted(merged.values(), key=lambda r: -r["score"])
    return out


def dedup(rows: list[dict], k: int) -> list[dict]:
    """Collapse byte-identical passages, keeping the best-ranked copy.

    Every copy stays in the index because each must remain citable; showing the
    same paragraph three times in one answer's evidence is what we avoid here.
    """
    kept: dict[str, dict] = {}
    out: list[dict] = []
    for r in rows:
        h = r.get("content_hash") or r.get("chunk_id", "")
        first = kept.get(h)
        if first is not None:
            # Record the other files the same passage appears in — a claim
            # carried by three publications is worth seeing as such.
            first.setdefault("duplicates", []).append(r.get("citation", ""))
            continue
        if len(out) >= k:
            continue
        kept[h] = r
        out.append(r)
    return out


def search_passages(question: str, k: int = 12, *,
                    filters: Optional[Filters] = None,
                    use_vector: bool = True,
                    use_keyword: bool = True) -> tuple[list[dict], dict[str, Any]]:
    """Fused passage retrieval. Returns `(passages, diagnostics)`.

    Never raises for a missing leg: the FTS index is optional and the embedding
    model needs the `.[local]` extra, so a machine can legitimately have one and
    not the other.
    """
    filters = filters or Filters()
    where = filters.where()
    diag: dict[str, Any] = {"where": where}
    fetch = max(k * OVERFETCH, k)

    try:
        table = vstore.open_table()
    except FileNotFoundError as e:
        diag["vector"] = diag["keyword"] = f"unavailable: {e}"
        return [], diag

    legs: dict[str, list[dict]] = {}
    if use_vector:
        try:
            legs["vector"] = vector_leg(table, question, fetch, where)
            diag["vector"] = len(legs["vector"])
        except vstore.EmbeddingDimensionError:
            # Falling back to BM25 makes a model/index deployment mistake look
            # like a healthy hybrid search with mysteriously poor multilingual
            # recall. Fail startup/request loudly with the actionable message.
            raise
        except Exception as e:                      # missing model, bad filter
            diag["vector"] = f"failed: {type(e).__name__}: {e}"
    if use_keyword:
        try:
            legs["keyword"] = keyword_leg(table, question, fetch, where)
            diag["keyword"] = len(legs["keyword"])
        except Exception as e:                      # no FTS index on this copy
            diag["keyword"] = f"failed: {type(e).__name__}: {e}"

    # The 1024-dim embedding is dead weight past this point: it bloats `--json`
    # output and every debug print of a retrieved row.
    for rows in legs.values():
        for row in rows:
            row.pop("vector", None)

    fused = fuse(legs)
    ranked = rerank(question, fused)
    unique = dedup(ranked, len(ranked))
    passages = diversify(unique, k)
    for i, p in enumerate(passages, start=1):
        p["rank"] = i
    diag["fused"] = len(fused)
    diag["rerank"] = "archive-source-prior-v1"
    diag["rerank_intent"] = passages[0].get("rerank_intent") if passages else None
    diag["unique"] = len(unique)
    return passages, diag


def ensure_fts_index() -> str:
    """Build the BM25 index if this copy of `index/lancedb` lacks one.

    Free and local (~seconds over 4267 rows). Kept as an explicit call rather
    than an implicit one inside `search_passages` so a query never silently
    rewrites the shared index directory.
    """
    table = vstore.open_table()
    vstore.ensure_fts(table)
    return f"fts index built over {table.count_rows()} rows"
