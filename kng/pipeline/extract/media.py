"""Media extractors: images -> Sarvam OCR, videos -> Sarvam ASR.

Both are gated: with use_ocr/use_asr False they return [] so a dry run makes no
paid calls. Newspaper-clip OCR text is lightly cleaned so it comes out readable.
"""
from __future__ import annotations

import re
from pathlib import Path

from ...models import Locator, Segment, SourceType


def _clean_ocr(text: str) -> str:
    """Tidy OCR markdown from newspaper cuttings: collapse whitespace, drop
    stray single-char lines and repeated separators, keep headings/paragraphs."""
    text = text.replace("‌", "").replace("\xad", "")
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if not ln:
            continue
        if re.fullmatch(r"[-_=*·•|]{1,}", ln):   # separator noise
            continue
        if len(ln) == 1 and not ln.isalnum():
            continue
        lines.append(ln)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _seg(meta: dict, rel: str, sid: str, text: str, locator: Locator,
         stype: SourceType, speaker: str | None = None) -> Segment:
    return Segment(
        segment_id=sid,
        source_file=rel,
        source_type=stype,
        press_meet_id=meta["press_meet_id"],
        press_meet_title=meta["press_meet_title"],
        date=meta["date"],
        topic=meta["topic"],
        publication=meta["publication"],
        speaker=speaker,
        locator=locator,
        text_original=text.strip(),
    )


def extract_image(path: Path, meta: dict, rel: str, use_ocr: bool = True) -> list[Segment]:
    if not use_ocr:
        return []
    from ...providers import get_ocr
    ocr = get_ocr()
    segs: list[Segment] = []
    for page, raw in ocr.ocr_file(path, languages=["te", "hi", "en"]):
        text = _clean_ocr(raw)
        if text:
            segs.append(_seg(meta, rel, f"{rel}#ocr{page}", text,
                             Locator(page=page if page > 1 else None),
                             SourceType.news_clip))
    return segs


def extract_video(path: Path, meta: dict, rel: str, use_asr: bool = True) -> list[Segment]:
    if not use_asr:
        return []
    from ...providers import get_asr
    asr = get_asr()
    segs: list[Segment] = []
    for i, (start, end, text) in enumerate(asr.transcribe(path)):
        text = text.strip()
        if text:
            segs.append(_seg(meta, rel, f"{rel}#t{i}", text,
                             Locator(video_span=(round(start, 1), round(end, 1))),
                             SourceType.video, speaker="speaker"))
    return segs
