"""WP6 eval-harness tests — offline, deterministic, no index or model needed.

The harness is an instrument: if its scoring is wrong, every conclusion drawn
from it is wrong, and silently so. So the arithmetic is pinned on synthetic
contexts rather than trusting a number that came out of the real index.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kng.eval import harness
from kng.retrieval import Context


def passage(meet: str, citation: str = "f.pdf", **extra) -> dict:
    return {"press_meet_id": meet, "citation": citation,
            "source_type": "press_release", "language": "en", **extra}


def ctx_with(meets: list[str], entities: list[str] | None = None,
             facts: int = 0) -> Context:
    return Context(
        question="q",
        passages=[passage(m, f"file-{i}.pdf") for i, m in enumerate(meets)],
        facts=[{"relation": "R"} for _ in range(facts)],
        entities=[{"name": n} for n in (entities or [])])


class TestQuestionSet(unittest.TestCase):
    def test_the_shipped_set_loads_and_is_well_formed(self):
        questions = harness.load_questions()
        self.assertGreaterEqual(len(questions), 25)
        self.assertEqual(len({q.id for q in questions}), len(questions))
        for q in questions:
            self.assertTrue(q.q.strip(), q.id)
            self.assertIn(q.lang, ("en", "te"), q.id)
            self.assertIn(q.kind, ("single", "cross", "temporal", "numeric"), q.id)
            self.assertTrue(q.meets, f"{q.id} has no expected press meet")

    def test_both_scripts_are_represented(self):
        langs = {q.lang for q in harness.load_questions()}
        self.assertEqual(langs, {"en", "te"},
                         "a Telugu-dominant corpus must be evaluated in Telugu too")

    def test_duplicate_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / "q.yaml"
            fp.write_text("questions:\n"
                          "  - id: a\n    q: one\n    meets: ['1']\n"
                          "  - id: a\n    q: two\n    meets: ['1']\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                harness.load_questions(fp)

    def test_validate_flags_an_expectation_the_index_cannot_satisfy(self):
        questions = [harness.Question(id="x", q="q", meets=["3", "nope"])]
        with mock.patch.object(harness, "known_meets", return_value={"3"}):
            problems = harness.validate(questions)
        self.assertEqual(len(problems), 1)
        self.assertIn("nope", problems[0])


class TestRetrievalScoring(unittest.TestCase):
    def test_rank_recall_and_precision(self):
        q = harness.Question(id="x", q="q", meets=["10", "11"])
        # 4 passages: the first expected one is 3rd, and only meet 10 is covered.
        res = harness.score_retrieval(q, ctx_with(["1", "2", "10", "10"]))
        self.assertTrue(res.meet_hit)
        self.assertEqual(res.meet_rank, 3)
        self.assertAlmostEqual(res.reciprocal_rank, 1 / 3)
        self.assertAlmostEqual(res.meet_recall, 0.5)      # 10 found, 11 missing
        self.assertAlmostEqual(res.meet_precision, 0.5)   # 2 of 4 passages

    def test_a_complete_miss_scores_zero_and_keeps_the_top_for_the_report(self):
        q = harness.Question(id="x", q="q", meets=["9"])
        res = harness.score_retrieval(q, ctx_with(["1", "2", "3"]))
        self.assertFalse(res.meet_hit)
        self.assertIsNone(res.meet_rank)
        self.assertEqual(res.reciprocal_rank, 0.0)
        self.assertEqual(res.meet_recall, 0.0)
        self.assertEqual([t["meet"] for t in res.top], ["1", "2", "3"])

    def test_top_is_capped_at_five(self):
        q = harness.Question(id="x", q="q", meets=["1"])
        res = harness.score_retrieval(q, ctx_with([str(i) for i in range(12)]))
        self.assertEqual(len(res.top), 5)

    def test_entity_matching_works_in_both_directions(self):
        # "TTD" must match the node "TTD Laddu", and "laddu" must match it too.
        q = harness.Question(id="x", q="q", meets=["1"], entities=["TTD"])
        self.assertTrue(harness.score_retrieval(q, ctx_with(["1"], ["TTD Laddu"])).entity_hit)
        q2 = harness.Question(id="x", q="q", meets=["1"], entities=["laddu"])
        self.assertTrue(harness.score_retrieval(q2, ctx_with(["1"], ["TTD Laddu"])).entity_hit)
        q3 = harness.Question(id="x", q="q", meets=["1"], entities=["SECI"])
        self.assertFalse(harness.score_retrieval(q3, ctx_with(["1"], ["TTD Laddu"])).entity_hit)

    def test_facts_are_counted(self):
        q = harness.Question(id="x", q="q", meets=["1"])
        self.assertEqual(harness.score_retrieval(q, ctx_with(["1"], facts=7)).facts, 7)


class TestAnswerScoring(unittest.TestCase):
    class FakeAnswer:
        def __init__(self, text, cited, sources, invalid=(), uncited=0,
                     grounding_passed=True, refused=False):
            self.text, self.cited, self.sources = text, cited, sources
            self.invalid_citations, self.uncited_sentences = list(invalid), uncited
            self.grounding_passed = grounding_passed
            self.refused = refused
            self.refusal_reason = "insufficient" if refused else ""

    def test_cited_expected_meet_is_the_strong_signal(self):
        q = harness.Question(id="x", q="q", meets=["10"])
        res = harness.score_retrieval(q, ctx_with(["10", "2"]))
        sources = [{"n": 1, "press_meet_id": "2"}, {"n": 2, "press_meet_id": "10"}]

        # Cited [1] only — an off-target source, so the answer leaned on the wrong meet.
        only_wrong = harness.score_answer(q, res, self.FakeAnswer("a [1]", [1], sources))
        self.assertFalse(only_wrong.cited_expected_meet)

        res2 = harness.score_retrieval(q, ctx_with(["10", "2"]))
        right = harness.score_answer(q, res2, self.FakeAnswer("a [2]", [2], sources))
        self.assertTrue(right.cited_expected_meet)

    def test_stripped_citations_and_uncited_sentences_are_carried(self):
        q = harness.Question(id="x", q="q", meets=["1"])
        res = harness.score_retrieval(q, ctx_with(["1"]))
        out = harness.score_answer(q, res, self.FakeAnswer(
            "text", [1], [{"n": 1, "press_meet_id": "1"}], invalid=[9], uncited=3))
        self.assertTrue(out.answered)
        self.assertEqual(out.invalid_citations, [9])
        self.assertEqual(out.uncited_sentences, 3)
        self.assertEqual(out.answer_chars, 4)

    def test_refusal_is_recorded_as_a_failed_grounding_outcome(self):
        q = harness.Question(id="x", q="q", meets=["1"])
        res = harness.score_retrieval(q, ctx_with(["1"]))
        out = harness.score_answer(q, res, self.FakeAnswer(
            "grounded refusal", [], [], grounding_passed=False, refused=True))
        self.assertTrue(out.answered)
        self.assertTrue(out.refused)
        self.assertFalse(out.grounding_passed)
        self.assertEqual(out.refusal_reason, "insufficient")


class TestAggregate(unittest.TestCase):
    def results(self):
        hit = harness.QuestionResult(id="a", question="q", lang="en", kind="single",
                                     meet_hit=True, meet_rank=1, reciprocal_rank=1.0,
                                     meet_recall=1.0, facts=10)
        miss = harness.QuestionResult(id="b", question="q", lang="te", kind="cross",
                                      meet_hit=False, reciprocal_rank=0.0, facts=0)
        return [hit, miss]

    def test_means_and_misses(self):
        agg = harness.aggregate(self.results())
        self.assertEqual(agg["questions"], 2)
        self.assertAlmostEqual(agg["meet_hit_rate"], 0.5)
        self.assertAlmostEqual(agg["mrr"], 0.5)
        self.assertEqual(agg["misses"], ["b"])

    def test_cuts_by_kind_and_script(self):
        agg = harness.aggregate(self.results())
        self.assertEqual(agg["by_kind"]["single"]["meet_hit_rate"], 1.0)
        self.assertEqual(agg["by_kind"]["cross"]["meet_hit_rate"], 0.0)
        self.assertEqual(agg["by_lang"]["te"]["n"], 1)

    def test_answer_section_appears_only_when_answers_ran(self):
        self.assertNotIn("answers", harness.aggregate(self.results()))
        answered = self.results()
        answered[0].answered = True
        answered[0].cited = 4
        answered[0].grounding_passed = True
        metrics = harness.aggregate(answered)["answers"]
        self.assertEqual(metrics["mean_cited"], 4.0)
        self.assertEqual(metrics["grounding_pass_rate"], 1.0)
        self.assertEqual(metrics["refusal_rate"], 0.0)


class TestRunAndReport(unittest.TestCase):
    def test_run_scores_every_question_without_touching_the_index(self):
        questions = [harness.Question(id="a", q="q1", meets=["10"]),
                     harness.Question(id="b", q="q2", meets=["11"])]
        with mock.patch.object(harness, "retrieve",
                               side_effect=[ctx_with(["10"]), ctx_with(["1"])]):
            report = harness.run(questions, k=4)
        self.assertEqual([r.meet_hit for r in report.results], [True, False])
        self.assertEqual(report.config["k"], 4)
        self.assertAlmostEqual(report.aggregate["meet_hit_rate"], 0.5)

    def test_a_failing_question_is_recorded_not_raised(self):
        """One broken question must not lose the other 29 results."""
        questions = [harness.Question(id="boom", q="q", meets=["1"]),
                     harness.Question(id="ok", q="q", meets=["1"])]
        with mock.patch.object(harness, "retrieve",
                               side_effect=[RuntimeError("index gone"), ctx_with(["1"])]):
            report = harness.run(questions)
        self.assertIn("RuntimeError", report.results[0].error)
        self.assertTrue(report.results[1].meet_hit)
        self.assertEqual(report.aggregate["errors"], 1)

    def test_markdown_reports_metrics_misses_and_deltas(self):
        questions = [harness.Question(id="a", q="q", meets=["10"])]
        with mock.patch.object(harness, "retrieve", return_value=ctx_with(["2"])):
            report = harness.run(questions)
        with mock.patch.object(harness, "load_questions", return_value=questions):
            text = harness.markdown(report, baseline={"aggregate": {"meet_hit_rate": 0.5}})
        self.assertIn("meet hit rate", text)
        self.assertIn("(-0.500)", text)          # 0.0 now vs 0.5 before
        self.assertIn("Misses", text)
        self.assertIn("| a |", text)

    def test_save_writes_both_formats(self):
        questions = [harness.Question(id="a", q="q", meets=["10"])]
        with mock.patch.object(harness, "retrieve", return_value=ctx_with(["10"])):
            report = harness.run(questions)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(harness, "load_questions", return_value=questions):
                js, md = harness.save(report, out_dir=Path(tmp))
            self.assertTrue(js.exists() and md.exists())
            blob = json.loads(js.read_text(encoding="utf-8"))
            self.assertEqual(blob["aggregate"]["questions"], 1)
            self.assertIn("KNG eval", md.read_text(encoding="utf-8"))


class TestSpendGate(unittest.TestCase):
    """`--answer` costs one call per question, so it must not be easy to trip."""

    def _main(self, argv):
        from kng.eval.__main__ import main
        return main(argv)

    def test_answer_against_a_real_provider_needs_spend(self):
        class RealLLM:
            model = "sarvam-105b"
        with mock.patch("kng.providers.get_llm", return_value=RealLLM()), \
             mock.patch.object(harness, "validate", return_value=[]):
            self.assertEqual(self._main(["--answer", "--only", "laddu-en"]), 2)

    def test_retrieval_only_needs_no_gate(self):
        questions = [harness.Question(id="laddu-en", q="q", meets=["3"])]
        with mock.patch.object(harness, "load_questions", return_value=questions), \
             mock.patch.object(harness, "validate", return_value=[]), \
             mock.patch.object(harness, "retrieve", return_value=ctx_with(["3"])):
            self.assertEqual(self._main(["--no-save"]), 0)

    def test_an_unknown_question_id_is_an_error(self):
        with mock.patch.object(harness, "validate", return_value=[]):
            self.assertEqual(self._main(["--only", "no-such-question"]), 1)

    def test_a_stale_expectation_stops_the_run(self):
        with mock.patch.object(harness, "known_meets", return_value={"3"}):
            self.assertEqual(self._main(["--only", "seci-tariff"]), 1)


if __name__ == "__main__":
    unittest.main()
