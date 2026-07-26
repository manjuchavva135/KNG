"""WP5 API tests — auth, SSE contract, filters, path safety.

Offline: `KNG_FAKE_LLM=1` swaps in the fixture provider, so no key and no network
are needed. State (`users.json`, history, query log) goes to a temp dir via
`KNG_VAR_DIR`, which `auth.var_dir()` reads at call time precisely so a test can
redirect it after import.

The security cases here are the point of the file: an unauthenticated caller must
be refused, a normal user must not reach admin routes, and `/api/raw` must not
serve anything outside the data root.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("KNG_FAKE_LLM", "1")
os.environ.setdefault("KNG_SESSION_SECRET", "test-secret-for-unit-tests-only")

_TMP = tempfile.TemporaryDirectory()
os.environ["KNG_VAR_DIR"] = _TMP.name

from fastapi.testclient import TestClient   # noqa: E402

from kng.api import auth, sources           # noqa: E402
from kng.api.main import app                # noqa: E402

ADMIN = ("admin@example.com", "admin-password-1")
USER = ("reader@example.com", "reader-password-1")


def setUpModule() -> None:
    auth.add_user(ADMIN[0], ADMIN[1], role="admin")
    auth.add_user(USER[0], USER[1], role="user")


def tearDownModule() -> None:
    _TMP.cleanup()


def client_for(credentials=None) -> TestClient:
    c = TestClient(app)
    if credentials:
        res = c.post("/api/login", json={"email": credentials[0],
                                         "password": credentials[1]})
        assert res.status_code == 200, res.text
    return c


class TestAuth(unittest.TestCase):
    def test_unauthenticated_is_refused(self):
        c = TestClient(app)
        self.assertEqual(c.get("/api/meta").status_code, 401)
        self.assertEqual(c.get("/api/history").status_code, 401)
        self.assertEqual(c.post("/api/ask", json={"question": "hi"}).status_code, 401)

    def test_health_needs_no_session(self):
        self.assertEqual(TestClient(app).get("/api/health").status_code, 200)

    def test_bad_password_sets_no_cookie(self):
        c = TestClient(app)
        res = c.post("/api/login", json={"email": ADMIN[0], "password": "wrong"})
        self.assertEqual(res.status_code, 401)
        self.assertNotIn(auth.COOKIE_NAME, c.cookies)

    def test_unknown_and_wrong_password_are_indistinguishable(self):
        c = TestClient(app)
        a = c.post("/api/login", json={"email": "nobody@example.com", "password": "x" * 12})
        b = c.post("/api/login", json={"email": ADMIN[0], "password": "y" * 12})
        self.assertEqual(a.status_code, b.status_code)
        self.assertEqual(a.json()["detail"], b.json()["detail"])

    def test_login_then_me(self):
        c = client_for(ADMIN)
        self.assertEqual(c.get("/api/me").json()["user"]["email"], ADMIN[0])

    def test_logout_clears_session(self):
        c = client_for(USER)
        c.post("/api/logout")
        self.assertEqual(c.get("/api/me").status_code, 401)

    def test_tampered_token_is_rejected(self):
        c = client_for(USER)
        token = c.cookies.get(auth.COOKIE_NAME)
        body, sig = token.split(".", 1)
        c.cookies.set(auth.COOKIE_NAME, f"{body}.{'0' * len(sig)}")
        self.assertEqual(c.get("/api/me").status_code, 401)

    def test_disabled_user_loses_access_with_a_live_cookie(self):
        c = client_for(USER)
        self.assertEqual(c.get("/api/me").status_code, 200)
        auth.set_disabled(USER[0], True)
        try:
            # The cookie is still validly signed; the user record is re-read.
            self.assertEqual(c.get("/api/me").status_code, 401)
        finally:
            auth.set_disabled(USER[0], False)

    def test_password_is_not_stored_in_plaintext(self):
        blob = auth.users_file().read_text(encoding="utf-8")
        self.assertNotIn(ADMIN[1], blob)
        self.assertNotIn(USER[1], blob)


class TestAdminGating(unittest.TestCase):
    def test_normal_user_cannot_reach_admin_routes(self):
        c = client_for(USER)
        self.assertEqual(c.get("/api/admin/users").status_code, 403)
        self.assertEqual(c.get("/api/admin/stats").status_code, 403)

    def test_admin_can_list_users(self):
        c = client_for(ADMIN)
        emails = {u["email"] for u in c.get("/api/admin/users").json()["users"]}
        self.assertIn(USER[0], emails)

    def test_admin_cannot_disable_self(self):
        c = client_for(ADMIN)
        res = c.post("/api/admin/users/disable",
                     json={"email": ADMIN[0], "disabled": True})
        self.assertEqual(res.status_code, 400)


class TestMeta(unittest.TestCase):
    def test_meta_reports_real_coverage(self):
        meta = client_for(USER).get("/api/meta").json()
        self.assertEqual(len(meta["press_meets"]), 33)
        self.assertEqual(meta["chunks"], 4267)
        self.assertEqual(meta["coverage"]["start"], "2024-06-04")
        self.assertEqual(meta["coverage"]["end"], "2026-07-21")
        self.assertGreater(meta["graph"]["nodes"], 0)


class TestAskStream(unittest.TestCase):
    def _events(self, body: dict) -> list[tuple[str, dict]]:
        c = client_for(USER)
        out: list[tuple[str, dict]] = []
        with c.stream("POST", "/api/ask", json=body) as res:
            self.assertEqual(res.status_code, 200)
            frame: list[str] = []
            for line in res.iter_lines():
                if line:
                    frame.append(line)
                    continue
                event = next((l[6:].strip() for l in frame if l.startswith("event:")), None)
                data = next((l[5:].strip() for l in frame if l.startswith("data:")), None)
                frame = []
                if event and data:
                    out.append((event, json.loads(data)))
        return out

    def test_sources_precede_deltas_and_final_is_last(self):
        events = self._events({"question": "Tirupati laddu adulteration", "k": 3})
        kinds = [e for e, _ in events]
        self.assertIn("sources", kinds)
        self.assertIn("final", kinds)
        self.assertEqual(kinds[-1], "final")
        self.assertLess(kinds.index("sources"), kinds.index("delta"))

    def test_final_cites_only_real_sources(self):
        events = self._events({"question": "SECI solar tariff", "k": 3})
        final = dict(events)["final"]
        valid = {s["n"] for s in final["sources"]}
        self.assertTrue(set(final["cited"]).issubset(valid))
        self.assertEqual(final["invalid_citations"], [])

    def test_empty_question_is_rejected(self):
        c = client_for(USER)
        self.assertEqual(c.post("/api/ask", json={"question": "   "}).status_code, 400)

    def test_answer_is_written_to_history(self):
        events = self._events({"question": "liquor scam allegations", "k": 2})
        session_id = dict(events)["final"]["session_id"]
        c = client_for(USER)
        sessions = c.get("/api/history").json()["sessions"]
        self.assertIn(session_id, {s["session_id"] for s in sessions})
        turns = c.get(f"/api/history/{session_id}").json()["turns"]
        self.assertEqual(turns[0]["question"], "liquor scam allegations")


class TestFilters(unittest.TestCase):
    def test_body_maps_to_retrieval_filters(self):
        from kng.api.main import AskBody
        body = AskBody(question="q", press_meet_id="10", source_type="news_clip",
                       since="2024-09-01", until="2024-10-31")
        where = body.filters().where()
        self.assertIn("press_meet_id = '10'", where)
        self.assertIn("source_type = 'news_clip'", where)
        self.assertIn("date >= '2024-09-01'", where)
        self.assertIn("date <= '2024-10-31'", where)

    def test_k_is_bounded(self):
        c = client_for(USER)
        self.assertEqual(c.post("/api/ask", json={"question": "x", "k": 999}).status_code, 422)


class TestSourceSafety(unittest.TestCase):
    def test_traversal_is_refused(self):
        c = client_for(USER)
        for attempt in ("../../etc/passwd", "/etc/passwd",
                        "data/../../etc/passwd", "../.env"):
            self.assertEqual(c.get("/api/raw", params={"file": attempt}).status_code, 404,
                             f"{attempt} was not refused")
            self.assertEqual(c.get("/api/source", params={"file": attempt}).status_code, 404)

    def test_raw_refuses_paths_outside_the_data_root(self):
        # A real repo file that is not under data/ must still be refused.
        with self.assertRaises(sources.SourceNotFound):
            sources.raw_file("README.md")

    def test_safe_under_blocks_escape(self):
        with self.assertRaises(sources.SourceNotFound):
            sources._safe_under("../../etc/passwd", Path("index/chunks"))

    def test_known_source_resolves_to_its_passage(self):
        c = client_for(USER)
        with c.stream("POST", "/api/ask",
                      json={"question": "Tirupati laddu", "k": 3}) as res:
            payload = None
            frame: list[str] = []
            for line in res.iter_lines():
                if line:
                    frame.append(line)
                    continue
                event = next((l[6:].strip() for l in frame if l.startswith("event:")), None)
                data = next((l[5:].strip() for l in frame if l.startswith("data:")), None)
                frame = []
                if event == "sources" and data:
                    payload = json.loads(data)
                    break
        passages = [s for s in (payload or []) if s.get("kind") == "passage" and s.get("source_file")]
        self.assertTrue(passages, "no passage source came back to resolve")
        src = passages[0]
        self.assertTrue(src["chunk_id"], "sources must carry chunk_id so the "
                                         "viewer can open the cited passage")
        got = c.get("/api/source", params={"file": src["source_file"],
                                          "chunk_id": src["chunk_id"]})
        self.assertEqual(got.status_code, 200)
        body = got.json()
        self.assertTrue(body["text"])
        # The viewer must land on the passage that was cited, not the file's first.
        self.assertEqual(body["chunk_id"], src["chunk_id"])
        self.assertEqual(body["citation"], src["citation"])


if __name__ == "__main__":
    unittest.main()
