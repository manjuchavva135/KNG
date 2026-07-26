"""Central configuration. Reads `.env` once and exposes a typed Settings object.

Keeping this in one place (rather than scattering os.environ lookups) is what
makes providers swappable and the whole system reconfigurable for the target
deployment system without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of the `kng` package dir.
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # providers
    llm_provider: str = _env("LLM_PROVIDER", "sarvam")
    embed_provider: str = _env("EMBED_PROVIDER", "local")
    ocr_provider: str = _env("OCR_PROVIDER", "sarvam")
    asr_provider: str = _env("ASR_PROVIDER", "sarvam")
    translate_provider: str = _env("TRANSLATE_PROVIDER", "sarvam")
    rerank_provider: str = _env("RERANK_PROVIDER", "none")

    # sarvam
    sarvam_api_key: str = _env("SARVAM_API_KEY")
    # 'sarvam-m' is deprecated — the API rejects it with HTTP 400. 105b is the
    # only chat model verified to return tool calls for graph extraction.
    sarvam_chat_model: str = _env("SARVAM_CHAT_MODEL", "sarvam-105b")
    sarvam_ocr_model: str = _env("SARVAM_OCR_MODEL", "sarvam-vision")
    sarvam_asr_model: str = _env("SARVAM_ASR_MODEL", "saarika:v2.5")
    sarvam_translate_model: str = _env("SARVAM_TRANSLATE_MODEL", "mayura:v1")

    # anthropic (optional LLM)
    anthropic_api_key: str = _env("ANTHROPIC_API_KEY")
    anthropic_model: str = _env("ANTHROPIC_MODEL", "claude-opus-4-8")

    # embeddings
    local_embed_model: str = _env("LOCAL_EMBED_MODEL", "intfloat/multilingual-e5-base")
    cohere_api_key: str = _env("COHERE_API_KEY")
    cohere_embed_model: str = _env("COHERE_EMBED_MODEL", "embed-multilingual-v3.0")

    # asr fallback
    whisper_model: str = _env("WHISPER_MODEL", "large-v3")

    # behaviour
    translate_to_en: bool = _bool("TRANSLATE_TO_EN", True)
    answer_language: str = _env("ANSWER_LANGUAGE", "auto")

    # graph build (WP3). Concurrency is the paid-throughput knob: extraction is
    # API-bound, so raising it shortens the run without touching cluster CPU.
    llm_concurrency: int = _int("LLM_CONCURRENCY", 4)
    llm_retries: int = _int("LLM_RETRIES", 4)
    llm_timeout: float = _float("LLM_TIMEOUT", 120.0)
    llm_seed: int = _int("LLM_SEED", 42)
    # Sarvam publishes 40 req/min for sarvam-30b/105b on the Starter tier
    # (per account, not per key); Pro is 60 and Business 120. Sit just under it.
    llm_rpm: int = _int("LLM_RPM", 35)
    # sarvam-30b/105b are reasoning models. Measured on real chunks, "low" beats
    # the "medium" default on BOTH quality and reliability (44 entities / 29
    # relations / 0 failures vs 25 / 4 / 1 over the same passages): less thinking
    # leaves more of the tier's 4096-token output cap for the answer. Setting it
    # to null disables reasoning entirely, which is 20x faster but skims badly
    # (4 entities where "low" finds 27) — not worth it for extraction.
    llm_reasoning_effort: str = _env("LLM_REASONING_EFFORT", "low")
    community_resolution: float = _float("COMMUNITY_RESOLUTION", 1.0)
    min_entity_mentions: int = _int("MIN_ENTITY_MENTIONS", 1)
    # Chunk selection for the paid pass — see graph_extract.select_chunks.
    # Comma-separated SourceType names to extract, or empty for all. 59% of the
    # paid units in this corpus are `source_doc` — third-party PDFs (court
    # orders, tariff filings, merit lists) rather than Jagan's own words — so
    # `press_release,news_clip,video,slide` buys the graph that answers the
    # project's questions for 40% of the calls. Excluded chunks stay searchable
    # in LanceDB and can be extracted later without re-billing anything.
    graph_source_types: str = _env("GRAPH_SOURCE_TYPES", "")
    graph_min_chunk_chars: int = _int("GRAPH_MIN_CHUNK_CHARS", 150)
    graph_max_tag_density: float = _float("GRAPH_MAX_TAG_DENSITY", 0.5)
    graph_max_chunks_per_file: int = _int("GRAPH_MAX_CHUNKS_PER_FILE", 60)
    # Split anything longer before extraction: past ~2.5k chars sarvam-105b
    # exhausts the tier's 4096-token output cap on reasoning and returns nothing.
    graph_max_chunk_chars: int = _int("GRAPH_MAX_CHUNK_CHARS", 2400)

    # stores / paths (all relative to ROOT unless absolute)
    data_root: str = _env("DATA_ROOT", "data")
    lancedb_path: str = _env("LANCEDB_PATH", "index/lancedb")
    graph_path: str = _env("GRAPH_PATH", "index/graph")
    graph_backend: str = _env("GRAPH_BACKEND", "networkx")
    neo4j_uri: str = _env("NEO4J_URI")
    neo4j_user: str = _env("NEO4J_USER", "neo4j")
    neo4j_password: str = _env("NEO4J_PASSWORD")

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else ROOT / p

    @property
    def data_dir(self) -> Path:
        return self.path(self.data_root)

    @property
    def index_dir(self) -> Path:
        return ROOT / "index"

    @property
    def extracted_dir(self) -> Path:
        return ROOT / "extracted"


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
