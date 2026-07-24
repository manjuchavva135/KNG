"""Derive press-meet metadata from folder & file names.

Folder convention in the archive:
    YS JAGAN_PRESSMEETS DATA/<id>_<DD.MM.YYYY>_<TOPIC>/...
    July 2026/<TOPIC>/...
Filenames additionally hint publication (Sakshi/Eenadu/Jyothi) and dates.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..models import SourceType

_DATE_DOTTED = re.compile(r"(\d{1,2})[.\-_](\d{1,2})[.\-_](20\d{2})")
_DATE_COMPACT = re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")   # DDMMYYYY

_PUBLICATIONS = {
    "sakshi": "Sakshi", "sak ": "Sakshi", " sak": "Sakshi", "_sak": "Sakshi",
    "eenadu": "Eenadu",
    "jyothi": "Andhra Jyothi", "jyoti": "Andhra Jyothi", "andhrajyothi": "Andhra Jyothi",
}

_EXT_TYPE = {
    ".docx": SourceType.press_release, ".doc": SourceType.press_release,
    ".rtf": SourceType.press_release,
    ".pdf": SourceType.source_doc,
    ".pptx": SourceType.slide, ".ppt": SourceType.slide,
    ".xlsx": SourceType.table, ".xls": SourceType.table, ".csv": SourceType.table,
    ".jpg": SourceType.news_clip, ".jpeg": SourceType.news_clip, ".png": SourceType.news_clip,
    ".mp4": SourceType.video, ".mov": SourceType.video, ".mkv": SourceType.video,
    ".m4a": SourceType.video, ".mp3": SourceType.video, ".wav": SourceType.video,
}


def _iso_date(text: str) -> str | None:
    m = _DATE_DOTTED.search(text)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    m = _DATE_COMPACT.search(text)
    if m:
        d, mo, y = m.groups()
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def detect_publication(name: str) -> str | None:
    low = f" {name.lower()} "
    for key, pub in _PUBLICATIONS.items():
        if key in low:
            return pub
    return None


def source_type_for(path: Path) -> SourceType | None:
    return _EXT_TYPE.get(path.suffix.lower())


def parse_meet_folder(folder: str) -> tuple[str, str | None, str]:
    """'10_28.11.2024_SECI - POWER SECTOR' -> ('10', '2024-11-28', 'SECI - POWER SECTOR')."""
    parts = folder.split("_")
    meet_id = parts[0].strip() if parts else folder
    date = _iso_date(folder)
    # topic = everything after the id and date token
    topic = folder
    if len(parts) >= 3:
        topic = "_".join(parts[2:]).strip()
    elif len(parts) == 2:
        topic = parts[1].strip()
    return meet_id, date, topic


def derive(abs_path: Path, data_root: Path) -> dict:
    """Return metadata dict for a source file."""
    rel = abs_path.relative_to(data_root)
    parts = rel.parts
    top = parts[0] if parts else ""            # 'YS JAGAN_PRESSMEETS DATA' or 'July 2026'
    meet_folder = parts[1] if len(parts) > 1 else top

    if top.lower().startswith("july"):
        meet_id = f"{top}/{meet_folder}"
        title = meet_folder
        topic = meet_folder
        date = _iso_date(str(rel))
    else:
        meet_id, date, topic = parse_meet_folder(meet_folder)
        title = meet_folder
        if date is None:
            date = _iso_date(str(rel))

    stype = source_type_for(abs_path)
    pub = detect_publication(abs_path.name) if stype == SourceType.news_clip else \
        detect_publication(abs_path.name)

    return {
        "press_meet_id": meet_id,
        "press_meet_title": title,
        "date": date,
        "topic": topic,
        "publication": pub,
        "source_type": stype,
    }
