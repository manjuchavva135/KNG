"""Extraction-cache and chunk-selection tests — the parts that decide what gets
billed.

Two invariants matter enough to pin down:

* **fixture output is never reusable by a paid run** — the guardrail that keeps
  `KNG_FAKE_LLM` results out of the real graph;
* **a config change does not invalidate paid work** — records are filtered per
  entry, so switching model or reasoning effort re-bills nothing.

Everything here is offline: temp dirs, no model, no network, no API key.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from kng.models import Chunk
from kng.pipeline import graph_extract as gx

FP_105B = f"SarvamLLM/sarvam-105b/{gx.PROMPT_VERSION}"
FP_30B = f"SarvamLLM/sarvam-30b/{gx.PROMPT_VERSION}"
FP_FAKE = f"FakeLLM/fake/{gx.PROMPT_VERSION}"
FP_OLD_PROMPT = "SarvamLLM/sarvam-105b/g1"


class TestFingerprintAcceptance(unittest.TestCase):
    def test_paid_records_are_acceptable_across_models(self):
        self.assertTrue(gx._fp_acceptable(FP_105B))
        self.assertTrue(gx._fp_acceptable(FP_30B))

    def test_fixture_and_old_prompt_are_not(self):
        self.assertFalse(gx._fp_acceptable(FP_FAKE))
        self.assertFalse(gx._fp_acceptable(FP_OLD_PROMPT))
        self.assertFalse(gx._fp_acceptable(None))
        self.assertFalse(gx._fp_acceptable("garbage"))

    def test_paid_run_reuses_other_paid_model_but_never_fixture(self):
        self.assertTrue(gx._reusable(FP_105B, FP_30B))
        self.assertTrue(gx._reusable(FP_30B, FP_105B))
        self.assertFalse(gx._reusable(FP_FAKE, FP_105B))
        self.assertFalse(gx._reusable(FP_OLD_PROMPT, FP_105B))

    def test_fixture_run_reuses_only_its_own(self):
        self.assertTrue(gx._reusable(FP_FAKE, FP_FAKE))
        self.assertFalse(gx._reusable(FP_105B, FP_FAKE))


class TestCacheRoundTrip(unittest.TestCase):
    """`load_cache`/`save_cache` against a real cache directory."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._patch = mock.patch.object(
            gx, "cache_path", lambda rel: Path(self._tmp.name) / (rel + ".json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, rel: str, blob: dict) -> None:
        p = Path(self._tmp.name) / (rel + ".json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(blob), encoding="utf-8")

    def test_legacy_file_level_fingerprint_still_loads(self):
        # Every cache file written before this change looks like this.
        self._write("f", {"fingerprint": FP_105B,
                          "records": {"h1": {"entities": [], "relations": []}}})
        self.assertEqual(list(gx.load_cache("f", FP_105B)), ["h1"])

    def test_legacy_paid_cache_survives_a_model_switch(self):
        self._write("f", {"fingerprint": FP_105B,
                          "records": {"h1": {"entities": [], "relations": []}}})
        loaded = gx.load_cache("f", FP_30B)
        self.assertEqual(list(loaded), ["h1"])
        # …and carries its true provenance forward, not the new run's.
        self.assertEqual(loaded["h1"]["_fp"], FP_105B)

    def test_mixed_file_drops_only_the_foreign_entries(self):
        self._write("f", {"fingerprint": FP_105B, "records": {
            "paid": {"entities": [], "relations": [], "_fp": FP_105B},
            "other_model": {"entities": [], "relations": [], "_fp": FP_30B},
            "fixture": {"entities": [], "relations": [], "_fp": FP_FAKE},
            "stale_prompt": {"entities": [], "relations": [], "_fp": FP_OLD_PROMPT},
        }})
        self.assertEqual(sorted(gx.load_cache("f", FP_105B)),
                         ["other_model", "paid"])

    def test_save_stamps_new_records_and_preserves_existing_provenance(self):
        records = {"new": {"entities": [], "relations": []},
                   "old": {"entities": [], "relations": [], "_fp": FP_105B}}
        gx.save_cache("f", records, FP_30B)
        blob = json.loads((Path(self._tmp.name) / "f.json").read_text(encoding="utf-8"))
        self.assertEqual(blob["records"]["new"]["_fp"], FP_30B)
        self.assertEqual(blob["records"]["old"]["_fp"], FP_105B)

    def test_corrupt_cache_is_discarded_not_raised(self):
        p = Path(self._tmp.name) / "f.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(gx.load_cache("f", FP_105B), {})


class TestMalformedModelOutput(unittest.TestCase):
    """The model does not always honour the schema. Nothing it returns may raise.

    Both defects seen in real passes killed or damaged a paid run: an entity
    missing `type` raised `KeyError` in `_merge_records` and aborted the whole
    pass, and a bare string where an object was expected raised `AttributeError`
    in `validate` and cost the unit.
    """

    def test_validate_survives_strings_where_objects_belong(self):
        from collections import Counter
        issues: Counter = Counter()
        out = gx.validate({
            "entities": ["YS Jagan", {"name": "YS Jagan", "type": "Person"},
                         {"name": "YSRCP", "type": "Party"}, None],
            "relations": ["junk", {"source": "YS Jagan", "relation": "MEMBER_OF",
                                   "target": "YSRCP"}],
        }, issues)
        self.assertEqual([e["name"] for e in out["entities"]], ["YS Jagan", "YSRCP"])
        self.assertEqual(len(out["relations"]), 1)
        self.assertEqual(issues["untyped_entity_string"], 2)

    def test_merge_records_survives_missing_keys(self):
        out = gx._merge_records([
            {"entities": [{"name": "A"}, {"type": "Person", "name": "Jagan"}, "junk"],
             "relations": [{"source": "Jagan", "relation": "MEMBER_OF",
                            "target": "YSRCP"}, {"source": "X", "target": "Y"}]},
            None,
        ])
        self.assertEqual([e["name"] for e in out["entities"]], ["Jagan"])
        self.assertEqual(len(out["relations"]), 1)


class TestSourceTypeScope(unittest.TestCase):
    def _chunks(self):
        def mk(i: int, stype: str) -> Chunk:
            return Chunk(chunk_id=f"c{i}", text="ఏపీ " * 120, text_original="x",
                         content_hash=f"h{i}", source_file="f.docx",
                         source_type=stype, press_meet_id="10")
        return {"f.docx": [mk(1, "press_release"), mk(2, "source_doc"),
                           mk(3, "news_clip"), mk(4, "table")]}

    def _select(self, scope: str):
        """Override the setting the way production cannot: in place.

        `Settings` evaluates every `_env(...)` in its class body, so the values
        are frozen when `kng.config` is imported — patching `os.environ` here
        would do nothing. Real runs therefore pass `GRAPH_SOURCE_TYPES=...` on
        the command line, before the import; tests swap the accessor instead.
        """
        from dataclasses import replace

        from kng.config import settings
        overridden = replace(settings(), graph_source_types=scope)
        with mock.patch.object(gx, "settings", lambda: overridden):
            return gx.select_chunks(self._chunks())

    def test_empty_scope_keeps_every_source_type(self):
        picked, skipped = self._select("")
        self.assertEqual(len(picked), 4)
        self.assertEqual(skipped["source_type"], 0)

    def test_scope_filters_and_is_counted(self):
        picked, skipped = self._select("press_release,news_clip")
        self.assertEqual({c.source_type for _, c in picked},
                         {"press_release", "news_clip"})
        self.assertEqual(skipped["source_type"], 2)

    def test_whitespace_in_scope_is_tolerated(self):
        picked, _ = self._select(" press_release , video ")
        self.assertEqual([c.chunk_id for _, c in picked], ["c1"])


if __name__ == "__main__":
    unittest.main()
