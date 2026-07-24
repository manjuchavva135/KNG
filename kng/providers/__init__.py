"""Provider abstraction — the modular seam.

Every heavy model (LLM, embeddings, OCR, ASR, translate) is reached through a
factory here. Swapping Sarvam <-> local <-> another cloud is a .env change, not
a code change. Factories are cached so clients/models load once.
"""
from __future__ import annotations

from functools import lru_cache

from ..config import settings


@lru_cache(maxsize=1)
def get_embedder():
    from .embeddings import CohereEmbedder, LocalEmbedder
    s = settings()
    if s.embed_provider == "cohere" and s.cohere_api_key:
        return CohereEmbedder(s.cohere_api_key, s.cohere_embed_model)
    return LocalEmbedder(s.local_embed_model)


@lru_cache(maxsize=1)
def get_llm():
    from .llm import AnthropicLLM, SarvamLLM
    s = settings()
    if s.llm_provider == "anthropic" and s.anthropic_api_key:
        return AnthropicLLM(s.anthropic_model)
    return SarvamLLM(s.sarvam_chat_model)


@lru_cache(maxsize=1)
def get_ocr():
    from .ocr import NoOCR, SarvamOCR, TesseractOCR
    s = settings()
    if s.ocr_provider == "sarvam" and s.sarvam_api_key:
        return SarvamOCR(s.sarvam_ocr_model)
    if s.ocr_provider == "tesseract":
        return TesseractOCR()
    return NoOCR()


@lru_cache(maxsize=1)
def get_asr():
    from .asr import NoASR, SarvamASR, WhisperASR
    s = settings()
    if s.asr_provider == "sarvam" and s.sarvam_api_key:
        return SarvamASR(s.sarvam_asr_model)
    if s.asr_provider == "whisper":
        return WhisperASR(s.whisper_model)
    return NoASR()


@lru_cache(maxsize=1)
def get_translator():
    from .translate import NoTranslator, SarvamTranslator
    s = settings()
    if s.translate_provider == "sarvam" and s.sarvam_api_key and s.translate_to_en:
        return SarvamTranslator(s.sarvam_translate_model)
    return NoTranslator()
