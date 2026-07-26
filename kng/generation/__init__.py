"""WP4 generation — turning retrieved evidence into a cited synopsis."""
from __future__ import annotations

from .synthesize import Answer, answer, build_prompt, build_sources

__all__ = ["Answer", "answer", "build_prompt", "build_sources"]
