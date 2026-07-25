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
