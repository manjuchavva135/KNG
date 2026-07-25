"""Segment → Chunk: token-aware, structure-aware, provenance-preserving splitting.

The archive is lopsided at both ends. Press releases arrive as a *single*
segment per document (median 18.5k chars, max 134k) and must be split; video ASR
spans arrive one utterance at a time (median 268 chars) and must be merged.
Everything in between — PDF pages, news clips, slides — is usually already a
sensible retrieval unit.

Two rules shape the design:

*Never truncate.* Chunks are measured with the **embedding model's own
tokenizer**, so a chunk cannot silently overflow the model and lose its tail.
(`multilingual-e5-base` would have capped at 512 tokens; `bge-m3` allows 8192.)

*Never blur a citation.* A chunk is split only *within* one segment, so it keeps
that segment's exact locator. Segments are merged only for video, where
consecutive ASR spans belong to one continuous utterance and the merged chunk
carries the full `start–end` timestamp range. Pages and slides are never merged,
because a chunk spanning `p.4`–`p.5` could not cite either exactly.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path

from ..config import ROOT, settings
from ..models import Chunk, ExtractedDoc, Locator, Segment, SourceType

CHUNKS_DIR = ROOT / "index" / "chunks"

# Token budget. bge-m3 allows 8192; ~1000 keeps a chunk topically tight enough to
# retrieve precisely while holding a whole argument from a press release.
TARGET_TOKENS = 1000
OVERLAP_TOKENS = 120
CEILING_TOKENS = 1400        # never emit a chunk longer than this
MIN_TAIL_TOKENS = 80         # fold a shorter trailing chunk back into its predecessor

# Break priorities: cut at the most structural boundary available in the window.
_HEADING = 3
_PARAGRAPH = 2
_SENTENCE = 1

_RE_HEADING = re.compile(r"(?m)^#{1,6} ")
_RE_PARAGRAPH = re.compile(r"\n[ \t]*\n")
# Telugu/Hindi danda alongside Latin terminators; require trailing whitespace so
# decimals and abbreviations ("Rs. 2,470") are not treated as sentence ends.
_RE_SENTENCE = re.compile(r"[.?!।॥]+[\"')\]]*\s")

# Fallback chars-per-token when the real tokenizer is unavailable. Measured on
# this corpus under bge-m3's XLM-R vocabulary: Telugu 4.6, English 5.5; set a
# little low so the estimate leans toward smaller chunks. It is an estimate, so
# unusual text can still exceed CEILING_TOKENS — harmless, because the ceiling
# is a tuning knob and the real limit (the model's 8192) keeps a wide margin.
# Install `.[local]` for exact counts.
_CPT_INDIC = 4.0
_CPT_LATIN = 5.0


@lru_cache(maxsize=1)
def _tokenizer():
    """The embedding model's tokenizer, or None to fall back to a char heuristic.

    Only the tokenizer is loaded — chunking stays runnable without torch.
    """
    try:
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(settings().local_embed_model)
    except Exception:
        return None


def _chars_per_token(text: str) -> float:
    indic = sum(1 for c in text[:2000] if 0x0900 <= ord(c) <= 0x0D7F)
    sample = min(len(text), 2000) or 1
    return _CPT_INDIC if indic / sample > 0.2 else _CPT_LATIN


def count_tokens(text: str) -> int:
    tok = _tokenizer()
    if tok is not None:
        return len(tok.encode(text, add_special_tokens=False))
    return int(len(text) / _chars_per_token(text)) + 1


def _token_offsets(text: str) -> list[tuple[int, int]] | None:
    """(start, end) char span of every token — one pass, so splitting is O(n)."""
    tok = _tokenizer()
    if tok is None:
        return None
    try:
        enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
        return [tuple(o) for o in enc["offset_mapping"]]
    except Exception:
        return None


def _structural_breaks(text: str) -> list[tuple[int, int]]:
    """Sorted (char_offset, priority) cut candidates, best-structure-first."""
    breaks: list[tuple[int, int]] = []
    for m in _RE_HEADING.finditer(text):
        breaks.append((m.start(), _HEADING))
    for m in _RE_PARAGRAPH.finditer(text):
        breaks.append((m.end(), _PARAGRAPH))
    for m in _RE_SENTENCE.finditer(text):
        breaks.append((m.end(), _SENTENCE))
    breaks.sort()
    return breaks


def _best_break(breaks: list[tuple[int, int]], lo: int, hi: int,
                prefer: int | None = None) -> int | None:
    """Most structural break in [lo, hi], nearest `prefer` among equal priority.

    Preferring the *latest* candidate instead would pin every chunk to the hard
    ceiling, since sentence ends are dense enough to always offer one there.
    """
    if prefer is None:
        prefer = hi
    best = None                      # (priority, -distance, offset)
    i = bisect_right(breaks, (lo, -1))
    while i < len(breaks) and breaks[i][0] <= hi:
        off, pri = breaks[i]
        key = (pri, -abs(off - prefer))
        if best is None or key > best[0]:
            best = (key, off)
        i += 1
    return best[1] if best else None


def _token_at_char(offsets: list[tuple[int, int]], char: int) -> int:
    """First token index at or after `char`."""
    lo, hi = 0, len(offsets)
    while lo < hi:
        mid = (lo + hi) // 2
        if offsets[mid][0] < char:
            lo = mid + 1
        else:
            hi = mid
    return lo


def split_text(text: str, target: int = TARGET_TOKENS, overlap: int = OVERLAP_TOKENS,
               ceiling: int = CEILING_TOKENS) -> list[str]:
    """Split `text` into <=ceiling-token pieces, cutting on structure where possible."""
    text = text.strip()
    if not text:
        return []
    offsets = _token_offsets(text)
    if offsets is None:
        return _split_by_chars(text, target, overlap)
    n = len(offsets)
    if n <= ceiling:
        return [text]

    breaks = _structural_breaks(text)
    pieces: list[str] = []
    i = 0
    while i < n:
        j = min(i + target, n)
        if j < n:
            # Prefer a cut close to the target; only widen toward the ceiling if
            # the tight window holds no structural boundary at all.
            want = offsets[j][0]
            cut = _best_break(breaks,
                              offsets[min(i + int(target * 0.6), n - 1)][0],
                              offsets[min(i + int(target * 1.25), n) - 1][1], want)
            if cut is None:
                cut = _best_break(breaks,
                                  offsets[min(i + target // 2, n - 1)][0],
                                  offsets[min(i + ceiling, n) - 1][1], want)
            if cut is not None:
                cand = _token_at_char(offsets, cut)
                if i < cand <= i + ceiling:
                    j = cand
        piece = text[offsets[i][0]:offsets[j - 1][1]].strip()
        if piece:
            pieces.append(piece)
        if j >= n:
            break
        # Rewind for overlap, then snap back to a boundary so the repeated text
        # starts at a sentence rather than mid-word (tokens are subwords).
        nxt = max(j - overlap, i + 1)
        snap = _best_break(breaks, offsets[max(nxt - overlap // 2, 0)][0],
                           offsets[nxt][0], offsets[nxt][0])
        if snap is not None:
            cand = _token_at_char(offsets, snap)
            if i < cand <= j:
                nxt = cand
        i = nxt

    # A stub tail retrieves poorly; fold it back if the merge stays legal.
    if len(pieces) > 1 and count_tokens(pieces[-1]) < MIN_TAIL_TOKENS:
        merged = pieces[-2] + "\n" + pieces[-1]
        if count_tokens(merged) <= ceiling:
            pieces[-2:] = [merged]
    return pieces


def _split_by_chars(text: str, target: int, overlap: int) -> list[str]:
    """Tokenizer-free fallback: same structural preference, char-estimated budget."""
    cpt = _chars_per_token(text)
    size, step = int(target * cpt), int((target - overlap) * cpt)
    if len(text) <= size:
        return [text]
    breaks = _structural_breaks(text)
    pieces, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = _best_break(breaks, start + size // 2, end)
            if cut and cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - int(overlap * cpt), start + 1)
    return pieces


def _hash(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()


def _to_chunk(seg: Segment, text: str, idx: int) -> Chunk:
    """Build a Chunk carrying every provenance field its Segment had.

    Language is re-detected per chunk rather than inherited: a long segment is
    often code-switched, so an English passage inside a Telugu-labelled document
    would otherwise answer a `--lang te` filter.
    """
    from .normalize import detect_language
    return Chunk(
        chunk_id=f"{seg.segment_id}#c{idx}",
        text=text,
        text_original=text,
        text_en=None,
        content_hash=_hash(text),
        source_file=seg.source_file,
        source_type=seg.source_type.value,
        press_meet_id=seg.press_meet_id,
        press_meet_title=seg.press_meet_title,
        date=seg.date,
        topic=seg.topic,
        publication=seg.publication,
        speaker=seg.speaker,
        language=detect_language(text) or seg.language,
        page=seg.locator.page,
        slide=seg.locator.slide,
        video_start=seg.locator.video_span[0] if seg.locator.video_span else None,
        video_end=seg.locator.video_span[1] if seg.locator.video_span else None,
        citation=seg.citation_label(),
    )


def _merge_video_spans(segments: list[Segment], target: int) -> list[Segment]:
    """Fuse consecutive ASR spans into ~target-token pseudo-segments.

    ASR fragments one continuous answer into many short spans; merging restores a
    retrievable unit. The merged span keeps the real start and end timestamps, so
    the citation still points at the right moment in the recording.
    """
    merged: list[Segment] = []
    bucket: list[Segment] = []

    def flush() -> None:
        if not bucket:
            return
        head = bucket[0]
        text = " ".join(s.text_original.strip() for s in bucket if s.text_original.strip())
        start = bucket[0].locator.video_span[0] if bucket[0].locator.video_span else None
        end = bucket[-1].locator.video_span[1] if bucket[-1].locator.video_span else None
        seg = head.model_copy(deep=True)
        seg.text_original = text
        seg.locator = Locator(video_span=(start, end) if start is not None else None)
        merged.append(seg)
        bucket.clear()

    running = 0
    for seg in segments:
        t = count_tokens(seg.text_original)
        if bucket and running + t > target:
            flush()
            running = 0
        bucket.append(seg)
        running += t
    flush()
    return merged


def chunk_doc(doc: ExtractedDoc) -> list[Chunk]:
    """All chunks for one extracted document, in reading order."""
    segments = [s for s in doc.segments if s.text_original.strip()]
    if not segments:
        return []
    if doc.source_type == SourceType.video:
        segments = _merge_video_spans(segments, TARGET_TOKENS)

    chunks: list[Chunk] = []
    for seg in segments:
        for i, piece in enumerate(split_text(seg.text_original)):
            chunks.append(_to_chunk(seg, piece, i))
    return chunks


# ── artifacts ──────────────────────────────────────────────────────────────────
def chunks_path(rel: str) -> Path:
    return CHUNKS_DIR / (rel + ".json")


def save_chunks(rel: str, chunks: list[Chunk]) -> None:
    fp = chunks_path(rel)
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = [c.model_dump(exclude={"embedding"}) for c in chunks]
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def load_chunks(rel: str) -> list[Chunk] | None:
    fp = chunks_path(rel)
    if not fp.exists():
        return None
    return [Chunk(**d) for d in json.loads(fp.read_text(encoding="utf-8"))]


def run_chunk(man, files: list[Path]) -> dict:
    """Chunk every extracted doc that needs it. Local only — no paid calls."""
    from ..stats import set_stage
    from .run import load_extracted

    counts: dict = {"total": len(files), "processed": 0, "skipped": 0, "errors": 0,
                    "segments": 0, "chunks": 0, "by_type": {}, "max_tokens": 0}
    for i, path in enumerate(files, start=1):
        rel = str(path.relative_to(ROOT))
        if not man.needs(rel, path, "chunk"):
            counts["skipped"] += 1
            continue
        doc = load_extracted(rel)
        if doc is None:
            counts["errors"] += 1
            continue
        chunks = chunk_doc(doc)
        save_chunks(rel, chunks)
        man.mark(rel, "chunk", "done")
        counts["processed"] += 1
        counts["segments"] += len(doc.segments)
        counts["chunks"] += len(chunks)
        t = doc.source_type.value
        counts["by_type"][t] = counts["by_type"].get(t, 0) + len(chunks)
        for c in chunks:
            counts["max_tokens"] = max(counts["max_tokens"], count_tokens(c.text))
        if i % 100 == 0:
            man.save()
    man.save()
    set_stage("chunk", counts)
    print(f"chunk: {counts['processed']}/{counts['total']} docs · "
          f"{counts['segments']} seg → {counts['chunks']} chunks · "
          f"max {counts['max_tokens']} tokens", file=sys.stderr)
    return counts
