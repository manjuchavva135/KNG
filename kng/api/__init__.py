"""WP5 — the PressMeets RAG web application.

`main.py` is the FastAPI app; `auth.py` handles users and sessions; `meta.py`
caches the corpus facts the filter sidebar needs; `sources.py` resolves a
citation back to its extracted text; `history.py` persists conversations.

Run it:

    python -m kng.api.users add --email you@example.com --admin
    KNG_SESSION_SECRET=$(openssl rand -hex 32) \\
      uvicorn kng.api.main:app --host 127.0.0.1 --port 8000
"""
