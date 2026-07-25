"""Typed data model shared across the pipeline.

The whole system is provenance-first: every Segment (and the Chunks derived from
it) carries enough locator info to render an exact citation — file, press-meet,
date, page/slide/timestamp, publication, speaker, language.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    press_release = "press_release"   # docx/doc — authoritative transcript
    source_doc = "source_doc"         # supporting pdf evidence
    news_clip = "news_clip"           # scanned newspaper image (OCR)
    video = "video"                   # mp4 press-meet recording (ASR)
    slide = "slide"                   # pptx deck
    table = "table"                   # xlsx


class Locator(BaseModel):
    """Where inside the source file this segment lives → drives the citation."""
    page: Optional[int] = None                 # pdf page / news-clip has None
    slide: Optional[int] = None                # pptx slide index
    video_span: Optional[tuple[float, float]] = None  # (start_s, end_s)
    para: Optional[int] = None                 # paragraph index within a doc


class Segment(BaseModel):
    """One logical extracted unit of text with its provenance."""
    segment_id: str
    source_file: str                           # path relative to project root
    source_type: SourceType
    press_meet_id: str                         # e.g. "10" or "IMP" or "july2026/Panchadarla"
    press_meet_title: str = ""
    date: Optional[str] = None                 # ISO YYYY-MM-DD when known
    topic: str = ""
    publication: Optional[str] = None          # Sakshi | Eenadu | Andhra Jyothi
    speaker: Optional[str] = None
    language: str = "unknown"                   # te | en | hi | mixed | unknown
    locator: Locator = Field(default_factory=Locator)
    text_original: str = ""
    text_en: Optional[str] = None              # English translation (optional)

    def citation_label(self) -> str:
        """Human-readable short citation, e.g.
        'Sakshi clip — SECI power (28.11.2024)' or
        'YS Jagan PC 11.03.2026.docx p.2'."""
        bits: list[str] = []
        name = self.source_file.split("/")[-1]
        if self.publication:
            bits.append(self.publication)
        bits.append(name)
        if self.locator.page is not None:
            bits.append(f"p.{self.locator.page}")
        if self.locator.slide is not None:
            bits.append(f"slide {self.locator.slide}")
        if self.locator.video_span is not None:
            s, e = self.locator.video_span
            bits.append(f"@ {int(s)//60:02d}:{int(s)%60:02d}")
        if self.date:
            bits.append(f"({self.date})")
        return " ".join(bits)


class ExtractedDoc(BaseModel):
    """Result of extracting one source file: its file-level metadata + segments."""
    source_file: str
    source_type: SourceType
    press_meet_id: str
    press_meet_title: str = ""
    date: Optional[str] = None
    topic: str = ""
    publication: Optional[str] = None
    file_hash: str = ""
    segments: list[Segment] = Field(default_factory=list)
    extractor: str = ""                         # which extractor produced this
    # paid Sarvam calls this file triggered, by kind: ocr | cleanup | asr | translate
    sarvam_calls: dict[str, int] = Field(default_factory=dict)
    error: Optional[str] = None


class Chunk(BaseModel):
    """Embed-ready unit. Metadata is flattened for vector-store payload filtering."""
    chunk_id: str
    text: str                                   # text used for embedding (original or EN)
    text_original: str
    text_en: Optional[str] = None
    embedding: Optional[list[float]] = None
    # sha1 of the whitespace-normalised text. The archive holds byte-identical
    # duplicate files, so the same passage legitimately appears under several
    # sources; every copy stays indexed (each must remain citable) and retrieval
    # collapses them on this hash instead.
    content_hash: str = ""
    # flattened provenance for filtering + citation
    source_file: str = ""
    source_type: str = ""
    press_meet_id: str = ""
    press_meet_title: str = ""
    date: Optional[str] = None
    topic: str = ""
    publication: Optional[str] = None
    speaker: Optional[str] = None
    language: str = "unknown"
    page: Optional[int] = None
    slide: Optional[int] = None
    video_start: Optional[float] = None
    video_end: Optional[float] = None
    citation: str = ""


# ── knowledge graph (WP3) ──────────────────────────────────────────────────────
# Node and edge types are declared in config/ontology.yaml and enforced by
# kng/graph/ontology.py; these carry the instances plus the provenance that makes
# every one of them citable.

class Entity(BaseModel):
    """A resolved graph node — one real-world thing, however it was spelled.

    `entity_id` is derived from (type, normalised canonical name) rather than
    assigned, so the same person found in a Telugu clip and an English press
    release lands on one node and a rebuild is reproducible.
    """
    entity_id: str
    name: str                                   # canonical display form
    type: str                                   # a node_type from the ontology
    aliases: list[str] = Field(default_factory=list)     # surface forms observed
    mention_count: int = 0
    press_meet_ids: list[str] = Field(default_factory=list)
    first_date: Optional[str] = None            # earliest meet mentioning it
    last_date: Optional[str] = None
    structural: bool = False                    # from file metadata, not the LLM


class Mention(BaseModel):
    """One occurrence of an entity in one chunk — the citation anchor.

    Without this the graph could assert a fact but not say where it came from,
    which is the whole point of the archive.
    """
    entity_id: str
    chunk_id: str
    source_file: str
    press_meet_id: str
    date: Optional[str] = None
    citation: str = ""
    surface: str = ""                           # the name exactly as written


class Relation(BaseModel):
    """A typed edge, with the passage that supports it."""
    source_id: str
    relation: str                               # a relationship_type from the ontology
    target_id: str
    evidence: str = ""                          # short supporting quote
    chunk_id: str = ""                          # "" for structural edges
    source_file: str = ""
    press_meet_id: str = ""
    date: Optional[str] = None
    citation: str = ""
    structural: bool = False

    def key(self) -> tuple[str, str, str]:
        return (self.source_id, self.relation, self.target_id)


class Community(BaseModel):
    """A Louvain cluster plus its LLM 'god-node' summary.

    These answer the global/thematic questions that passage retrieval cannot —
    "what recurs across the budget meets" rather than "what was said on the 20th".
    """
    community_id: str
    level: int = 0
    entity_ids: list[str] = Field(default_factory=list)
    press_meet_ids: list[str] = Field(default_factory=list)
    title: str = ""
    summary: str = ""
    size: int = 0
