"""Multilingual embedding providers (TE / HI / EN).

Sarvam has no embeddings API, so the default is a local sentence-transformers
model. The model id is deterministic → embeddings reproduce identically on the
target deployment system, which matters because the index is copied there.
"""
from __future__ import annotations

import numpy as np


class LocalEmbedder:
    """sentence-transformers multilingual model. CPU-friendly."""

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()
        # e5 models expect "query:"/"passage:" prefixes; handle transparently.
        self._is_e5 = "e5" in model_name.lower()

    def _prep(self, texts: list[str], kind: str) -> list[str]:
        if self._is_e5:
            prefix = "query: " if kind == "query" else "passage: "
            return [prefix + t for t in texts]
        return texts

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        prepped = self._prep(texts, kind)
        vecs = self._model.encode(
            prepped, normalize_embeddings=True, convert_to_numpy=True,
            show_progress_bar=False, batch_size=32,
        )
        return vecs.astype("float32")

    def embed_one(self, text: str, kind: str = "query") -> np.ndarray:
        return self.embed([text], kind=kind)[0]


class CohereEmbedder:
    """Cohere multilingual embeddings (cloud fallback)."""

    def __init__(self, api_key: str, model: str):
        import cohere
        self.model = model
        self._client = cohere.Client(api_key)
        self.dim = 1024  # embed-multilingual-v3.0

    def embed(self, texts: list[str], kind: str = "passage") -> np.ndarray:
        input_type = "search_query" if kind == "query" else "search_document"
        resp = self._client.embed(texts=texts, model=self.model, input_type=input_type)
        return np.asarray(resp.embeddings, dtype="float32")

    def embed_one(self, text: str, kind: str = "query") -> np.ndarray:
        return self.embed([text], kind=kind)[0]
