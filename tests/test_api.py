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
import time
import unittest
from pathlib import Path

os.environ.setdefault("KNG_FAKE_LLM", "1")
os.environ.setdefault("KNG_SESSION_SECRET", "test-secret-for-unit-tests-only")

_TMP = tempfile.TemporaryDirectory()
os.environ["KNG_VAR_DIR"] = _TMP.name

from fastapi.testclient import TestClient   # noqa: E402

from kng.api import auth, history, sources  # noqa: E402
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

    def test_changing_a_password_revokes_existing_sessions(self):
        """The reason `cred_version` exists.

        Without it a reset changed what the owner types and nothing else: a cookie
        taken before the reset kept working until it expired, while the admin who
        reset it believed the account was secured.
        """
        auth.add_user("rotate@example.com", "first-password", role="user")
        c = client_for(("rotate@example.com", "first-password"))
        self.assertEqual(c.get("/api/me").status_code, 200)

        auth.set_password("rotate@example.com", "second-password")
        self.assertEqual(c.get("/api/me").status_code, 401)

        # The new password still signs in, and its cookie works.
        fresh = client_for(("rotate@example.com", "second-password"))
        self.assertEqual(fresh.get("/api/me").status_code, 200)
        auth.delete_user("rotate@example.com")

    def test_deleting_an_account_kills_its_live_cookie(self):
        auth.add_user("ghost@example.com", "ghost-password", role="user")
        c = client_for(("ghost@example.com", "ghost-password"))
        self.assertEqual(c.get("/api/me").status_code, 200)
        auth.delete_user("ghost@example.com")
        self.assertEqual(c.get("/api/me").status_code, 401)

    def test_token_without_cred_version_is_treated_as_version_one(self):
        """Cookies issued before versioning existed must keep working."""
        user = auth.get_user(USER[0])
        payload = {"sub": user.id, "email": user.email, "role": user.role,
                   "exp": int(time.time()) + 3600}          # no "cv"
        body = auth._b64(json.dumps(payload, separators=(",", ":")).encode())
        import hashlib
        import hmac as _hmac
        sig = _hmac.new(auth._secret().encode(), body.encode(), hashlib.sha256).hexdigest()
        c = TestClient(app)
        c.cookies.set(auth.COOKIE_NAME, f"{body}.{sig}")
        self.assertEqual(c.get("/api/me").status_code, 200)

    def test_disabled_account_costs_the_same_time_as_a_wrong_password(self):
        """A fast rejection told an attacker the address exists and is switched off.

        The threshold is loose on purpose — this asserts the scrypt work happens
        at all, not a precise timing bound on shared hardware.
        """
        auth.add_user("slow@example.com", "slow-password", role="user")
        auth.set_disabled("slow@example.com", True)
        c = TestClient(app)

        def timed(email, password):
            start = time.perf_counter()
            res = c.post("/api/login", json={"email": email, "password": password})
            return time.perf_counter() - start, res

        wrong_t, wrong = timed(USER[0], "definitely-not-it")
        disabled_t, disabled = timed("slow@example.com", "slow-password")
        self.assertEqual(wrong.status_code, disabled.status_code)
        self.assertEqual(wrong.json()["detail"], disabled.json()["detail"])
        self.assertGreater(disabled_t, wrong_t * 0.4,
                           "the disabled path skipped the password hash, so its "
                           "response time identifies the account")
        auth.delete_user("slow@example.com")

    def test_login_throttle_counts_the_account_not_only_the_ip(self):
        auth.add_user("spray@example.com", "spray-password", role="user")
        try:
            c = TestClient(app)
            codes = {c.post("/api/login", json={"email": "spray@example.com",
                                                "password": "nope"}).status_code
                     for _ in range(auth._MAX_ATTEMPTS + 2)}
            self.assertIn(429, codes)
        finally:
            auth.clear_attempts("testclient", "spray@example.com")
            auth._attempts.clear()
            auth.delete_user("spray@example.com")


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

    def test_normal_user_cannot_reach_the_new_admin_routes(self):
        c = client_for(USER)
        for url, body in (("/api/admin/users/role", {"email": USER[0], "role": "admin"}),
                          ("/api/admin/users/password",
                           {"email": USER[0], "password": "hijacked-1234"}),
                          ("/api/admin/users/delete",
                           {"email": ADMIN[0], "confirm": ADMIN[0]})):
            self.assertEqual(c.post(url, json=body).status_code, 403, url)

    def test_admin_can_change_a_role(self):
        c = client_for(ADMIN)
        try:
            res = c.post("/api/admin/users/role", json={"email": USER[0], "role": "admin"})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["user"]["role"], "admin")
        finally:
            auth.set_role(USER[0], "user")

    def test_last_enabled_admin_cannot_be_demoted_disabled_or_deleted(self):
        """An instance with no admin can only be repaired from a shell on the host."""
        c = client_for(ADMIN)
        for url, body in (
                ("/api/admin/users/role", {"email": ADMIN[0], "role": "user"}),
                ("/api/admin/users/disable", {"email": ADMIN[0], "disabled": True}),
                ("/api/admin/users/delete", {"email": ADMIN[0], "confirm": ADMIN[0]})):
            res = c.post(url, json=body)
            self.assertEqual(res.status_code, 400, f"{url} was allowed")
        self.assertEqual(auth.get_user(ADMIN[0]).role, "admin")

    def test_admin_reset_password_signs_that_account_out(self):
        auth.add_user("reset-me@example.com", "old-password-1", role="user")
        victim = client_for(("reset-me@example.com", "old-password-1"))
        admin = client_for(ADMIN)
        res = admin.post("/api/admin/users/password",
                         json={"email": "reset-me@example.com",
                               "password": "new-password-1"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["sessions_revoked"])
        self.assertEqual(victim.get("/api/me").status_code, 401)
        auth.delete_user("reset-me@example.com")

    def test_delete_needs_the_address_echoed_back(self):
        auth.add_user("typo@example.com", "typo-password", role="user")
        c = client_for(ADMIN)
        res = c.post("/api/admin/users/delete",
                     json={"email": "typo@example.com", "confirm": "something-else"})
        self.assertEqual(res.status_code, 400)
        self.assertIsNotNone(auth.get_user("typo@example.com"))
        auth.delete_user("typo@example.com")

    def test_admin_cannot_delete_self(self):
        c = client_for(ADMIN)
        res = c.post("/api/admin/users/delete",
                     json={"email": ADMIN[0], "confirm": ADMIN[0]})
        self.assertEqual(res.status_code, 400)

    def test_delete_removes_the_account_and_its_history(self):
        doomed = auth.add_user("doomed@example.com", "doomed-password", role="user")
        history.append_turn(doomed.id, "s-1", {"question": "q", "answer": "a"})
        self.assertTrue(history.list_sessions(doomed.id))

        c = client_for(ADMIN)
        res = c.post("/api/admin/users/delete",
                     json={"email": "doomed@example.com",
                           "confirm": "Doomed@Example.com "})     # case/space tolerant
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["conversations_deleted"], 1)
        self.assertIsNone(auth.get_user("doomed@example.com"))
        self.assertEqual(history.list_sessions(doomed.id), [])
        self.assertFalse((auth.var_dir() / "history" / doomed.id).exists())

    def test_deleting_an_unknown_account_is_404(self):
        c = client_for(ADMIN)
        res = c.post("/api/admin/users/delete",
                     json={"email": "nobody@example.com", "confirm": "nobody@example.com"})
        self.assertEqual(res.status_code, 404)


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


class TestHistoryPage(unittest.TestCase):
    """The History view: search, rename, delete one, delete all, and isolation."""

    @classmethod
    def setUpClass(cls):
        cls.owner = auth.add_user("hist@example.com", "hist-password", role="user")
        cls.other = auth.add_user("nosy@example.com", "nosy-password", role="user")
        history.append_turn(cls.owner.id, "s-laddu", {
            "question": "Tirupati laddu ghee adulteration",
            "answer": "He said the ghee was adulterated [1].",
            "cited": [1], "sources": [{"n": 1}], "latency_s": 1.5,
            "uncited_sentences": 0, "invalid_citations": [],
        })
        history.append_turn(cls.owner.id, "s-solar", {
            "question": "SECI solar tariff",
            "answer": "The tariff dispute concerned SECI [1][2].",
            "cited": [1, 2], "sources": [{"n": 1}, {"n": 2}], "latency_s": 2.0,
            "uncited_sentences": 3, "invalid_citations": [9],
        })

    @classmethod
    def tearDownClass(cls):
        history.purge_user(cls.owner.id)
        history.purge_user(cls.other.id)
        auth.delete_user("hist@example.com")
        auth.delete_user("nosy@example.com")

    def client(self):
        return client_for(("hist@example.com", "hist-password"))

    def test_cards_carry_what_the_page_renders(self):
        sessions = self.client().get("/api/history").json()["sessions"]
        card = next(s for s in sessions if s["session_id"] == "s-solar")
        self.assertEqual(card["turns"], 1)
        self.assertEqual(card["cited"], 2)
        self.assertEqual(card["sources"], 2)
        self.assertEqual(card["uncited_sentences"], 3)
        self.assertEqual(card["stripped_citations"], 1)
        self.assertEqual(card["latency_s"], 2.0)
        self.assertIn("SECI", card["preview"])

    def test_search_matches_inside_the_conversation(self):
        c = self.client()
        # "adulterated" appears only in the answer text, never in a title.
        found = c.get("/api/history", params={"q": "adulterated"}).json()["sessions"]
        self.assertEqual([s["session_id"] for s in found], ["s-laddu"])
        self.assertEqual(c.get("/api/history", params={"q": "zzz"}).json()["sessions"], [])

    def test_rename_sticks_and_is_bounded(self):
        c = self.client()
        res = c.patch("/api/history/s-laddu", json={"title": "  Laddu thread  "})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["title"], "Laddu thread")
        titles = {s["session_id"]: s["title"]
                  for s in c.get("/api/history").json()["sessions"]}
        self.assertEqual(titles["s-laddu"], "Laddu thread")
        self.assertEqual(c.patch("/api/history/s-laddu", json={"title": ""}).status_code, 422)
        self.assertEqual(c.patch("/api/history/nope", json={"title": "x"}).status_code, 404)

    def test_one_users_history_is_invisible_to_another(self):
        nosy = client_for(("nosy@example.com", "nosy-password"))
        self.assertEqual(nosy.get("/api/history").json()["sessions"], [])
        # Guessing a session id must not reach across accounts.
        self.assertEqual(nosy.get("/api/history/s-laddu").status_code, 404)
        self.assertEqual(nosy.patch("/api/history/s-laddu",
                                    json={"title": "mine now"}).status_code, 404)
        self.assertEqual(nosy.delete("/api/history/s-laddu").json()["deleted"], False)
        # …and the owner still has it.
        self.assertEqual(self.client().get("/api/history/s-laddu").status_code, 200)

    def test_delete_one_then_all(self):
        owner = auth.add_user("clearme@example.com", "clear-password", role="user")
        for i in range(3):
            history.append_turn(owner.id, f"c-{i}", {"question": f"q{i}", "answer": "a"})
        c = client_for(("clearme@example.com", "clear-password"))
        self.assertEqual(len(c.get("/api/history").json()["sessions"]), 3)
        self.assertTrue(c.delete("/api/history/c-1").json()["deleted"])
        self.assertEqual(len(c.get("/api/history").json()["sessions"]), 2)
        self.assertEqual(c.delete("/api/history").json()["deleted"], 2)
        self.assertEqual(c.get("/api/history").json()["sessions"], [])
        history.purge_user(owner.id)
        auth.delete_user("clearme@example.com")


class TestPages(unittest.TestCase):
    def test_pages_render_for_a_signed_in_user(self):
        c = client_for(USER)
        for path in ("/", "/history", "/admin"):
            res = c.get(path)
            self.assertEqual(res.status_code, 200, path)
            self.assertIn("PressMeets", res.text)

    def test_index_redirects_to_login_when_signed_out(self):
        res = TestClient(app).get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/login")

    def test_login_page_redirects_when_already_signed_in(self):
        res = client_for(USER).get("/login", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/")

    def test_static_assets_are_served(self):
        c = client_for(USER)
        for asset in ("styles.css", "app.js", "history.js", "admin.js"):
            self.assertEqual(c.get(f"/static/{asset}").status_code, 200, asset)


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
