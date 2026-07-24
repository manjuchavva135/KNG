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
    sarvam_chat_model: str = _env("SARVAM_CHAT_MODEL", "sarvam-m")
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

    # stores / paths (all relative to ROOT unless absolute)
    data_root: str = _env("DATA_ROOT", "data")
    lancedb_path: str = _env("LANCEDB_PATH", "index/lancedb")
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
