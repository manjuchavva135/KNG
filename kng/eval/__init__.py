"""WP6 evaluation — score retrieval (free) and answers (paid) over a fixed set.

    python -m kng.eval                     # retrieval only: no key, no network, no spend
    python -m kng.eval -k 12 --no-graph    # ablations
    python -m kng.eval --answer --spend    # one paid LLM call per question
    KNG_FAKE_LLM=1 python -m kng.eval --answer   # same path, offline fixture, free
"""
from .harness import (Question, QuestionResult, Report, aggregate, known_meets,
                      load_questions, markdown, run, save, score_answer,
                      score_retrieval, validate)

__all__ = ["Question", "QuestionResult", "Report", "aggregate", "known_meets",
           "load_questions", "markdown", "run", "save", "score_answer",
           "score_retrieval", "validate"]
