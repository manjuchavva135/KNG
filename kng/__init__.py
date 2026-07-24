"""KNG — Hybrid GraphRAG over the YS Jagan press-meet archive.

Modular, provider-pluggable pipeline:
    raw files -> extract -> normalize -> chunk -> embed -> vector store
                                       \\-> graph extract -> graph store
Query: hybrid retrieval (vector + keyword + graph) -> grounded, cited synopsis.

All heavy models sit behind provider interfaces (kng.providers) so cloud
(Sarvam) and local fallbacks are swappable via .env. Output artifacts live in a
self-contained `index/` directory that can be copied to another system.
"""

__version__ = "0.1.0"
