"""WP4 unit tests — the pure logic, run with no model, no index and no network.

    python -m unittest discover -s tests -v

`unittest` rather than pytest so the suite needs no dependency the pipeline does
not already have. Everything here is deterministic: fusion order, citation
verification, filter escaping and entity linking are exactly the parts where a
silent regression would corrupt an answer's provenance rather than crash.
"""
from __future__ import annotations

import unittest
from unittest import mock

from kng.generation import synthesize
from kng.retrieval import hybrid
from kng.retrieval import graph_context as gctx


class TestFilters(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(hybrid.Filters().where())

    def test_combines_with_and(self):
        w = hybrid.Filters(language="te", since="2024-01-01").where()
        self.assertEqual(w, "language = 'te' AND date >= '2024-01-01'")

    def test_escapes_quotes(self):
        # A press-meet title with an apostrophe must not break the SQL literal.
        w = hybrid.Filters(press_meet_id="jagan's meet").where()
        self.assertEqual(w, "press_meet_id = 'jagan''s meet'")


class TestFusion(unittest.TestCase):
    def _row(self, cid: str) -> dict:
        return {"chunk_id": cid, "content_hash": cid, "text": cid, "citation": cid}

    def test_agreement_beats_single_leg_top_hit(self):
        # "b" is only second in each leg, but both legs found it; "x" and "y"
        # are each first in one leg and absent from the other.
        legs = {
            "vector": [self._row("x"), self._row("b"), self._row("c")],
            "keyword": [self._row("y"), self._row("b"), self._row("c")],
        }
        fused = hybrid.fuse(legs)
        self.assertEqual(fused[0]["chunk_id"], "b")
        self.assertEqual(fused[0]["ranks"], {"vector": 2, "keyword": 2})
        self.assertGreater(fused[0]["score"], fused[1]["score"])

    def test_single_leg_keeps_its_order(self):
        legs = {"vector": [self._row("a"), self._row("b")]}
        self.assertEqual([r["chunk_id"] for r in hybrid.fuse(legs)], ["a", "b"])

    def test_dedup_collapses_identical_passages(self):
        rows = [
            {"chunk_id": "1", "content_hash": "h", "citation": "fileA"},
            {"chunk_id": "2", "content_hash": "h", "citation": "fileB"},
            {"chunk_id": "3", "content_hash": "g", "citation": "fileC"},
        ]
        out = hybrid.dedup(rows, k=8)
        self.assertEqual([r["chunk_id"] for r in out], ["1", "3"])
        self.assertEqual(out[0]["duplicates"], ["fileB"])

    def test_dedup_records_duplicates_beyond_k(self):
        rows = [{"chunk_id": str(i), "content_hash": "h", "citation": f"f{i}"}
                for i in range(3)]
        out = hybrid.dedup(rows, k=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["duplicates"], ["f1", "f2"])

    def test_attribution_rerank_prefers_archive_statement_source(self):
        rows = [
            {"chunk_id": "doc", "score": 0.030, "text": "liquor allegations",
             "citation": "paper.pdf", "source_type": "source_doc"},
            {"chunk_id": "release", "score": 0.030, "text": "liquor allegations",
             "citation": "release.docx", "source_type": "press_release"},
        ]
        ranked = hybrid.rerank("What did Jagan say about the liquor allegations?", rows)
        self.assertEqual(ranked[0]["chunk_id"], "release")
        self.assertEqual(ranked[0]["rerank_intent"], "attribution")

    def test_documentary_rerank_prefers_primary_document(self):
        rows = [
            {"chunk_id": "release", "score": 0.030, "text": "CAG report",
             "citation": "release.docx", "source_type": "press_release"},
            {"chunk_id": "doc", "score": 0.030, "text": "CAG report",
             "citation": "report.pdf", "source_type": "source_doc"},
        ]
        ranked = hybrid.rerank("What did the CAG report conclude?", rows)
        self.assertEqual(ranked[0]["chunk_id"], "doc")
        self.assertEqual(ranked[0]["rerank_intent"], "documentary")

    def test_diversity_caps_one_file_but_fills_narrow_results(self):
        rows = [
            {"chunk_id": f"a{i}", "source_file": "a.pdf"} for i in range(4)
        ] + [{"chunk_id": "b", "source_file": "b.pdf"}]
        out = hybrid.diversify(rows, k=4, max_per_file=2)
        self.assertEqual([r["chunk_id"] for r in out[:3]], ["a0", "a1", "b"])
        self.assertEqual(len(out), 4)


class TestCitationVerification(unittest.TestCase):
    SOURCES = [{"n": 1}, {"n": 2}]

    def test_valid_citations_survive(self):
        text, cited, invalid, uncited = synthesize.verify_citations(
            "He alleged the tender was rigged [1]. The report disagrees [2].", self.SOURCES)
        self.assertEqual(cited, [1, 2])
        self.assertEqual(invalid, [])
        self.assertEqual(uncited, 0)
        self.assertIn("[1]", text)

    def test_invalid_citation_is_stripped_and_reported(self):
        text, cited, invalid, _ = synthesize.verify_citations(
            "A claim that no source supports at all, stated confidently [9].", self.SOURCES)
        self.assertEqual(invalid, [9])
        self.assertEqual(cited, [])
        self.assertNotIn("[9]", text)

    def test_mixed_group_keeps_only_valid_members(self):
        text, cited, invalid, _ = synthesize.verify_citations(
            "Both sources say the same thing about the tender [1, 7].", self.SOURCES)
        self.assertEqual((cited, invalid), ([1], [7]))
        self.assertIn("[1]", text)

    def test_uncited_sentence_is_counted(self):
        _, _, _, uncited = synthesize.verify_citations(
            "The tender was rigged, and this long sentence cites nothing at all.",
            self.SOURCES)
        self.assertEqual(uncited, 1)

    def test_headings_are_not_counted_as_uncited(self):
        _, _, _, uncited = synthesize.verify_citations(
            "## Details\nHe said the tender process was manipulated end to end [1].",
            self.SOURCES)
        self.assertEqual(uncited, 0)

    def test_short_factual_claim_is_not_exempt(self):
        _, _, _, uncited = synthesize.verify_citations(
            "The tender failed.", self.SOURCES)
        self.assertEqual(uncited, 1)

    def test_citation_after_terminal_period_is_canonicalised(self):
        text, cited, invalid, uncited = synthesize.verify_citations(
            "The tariff was Rs. 2.49. [1]", self.SOURCES)
        self.assertEqual((cited, invalid, uncited), ([1], [], 0))
        self.assertEqual(text, "The tariff was Rs. 2.49 [1].")


class TestGroundingGates(unittest.TestCase):
    class UnsupportedJudge:
        model = "judge"

        def complete_json(self, *args, **kwargs):
            return {
                "verdict": "unsupported",
                "unsupported_claims": [
                    {"claim_number": 1, "reason": "the cited text says no such thing"}
                ],
            }

    class MissingJudge:
        model = "judge"

        def complete_json(self, *args, **kwargs):
            return None

    def test_semantic_validator_fails_closed(self):
        passed, unsupported, _ = synthesize.validate_claim_support(
            self.UnsupportedJudge(), "The moon is cheese [1].",
            [{"n": 1, "citation": "f", "text": "The moon appeared in the sky."}])
        self.assertFalse(passed)
        self.assertIn("says no such thing", unsupported[0])

    def test_missing_evidence_judge_fails_closed(self):
        passed, detail = synthesize.validate_evidence_sufficiency(
            self.MissingJudge(), "question", [{"n": 1, "citation": "f", "text": "x"}])
        self.assertFalse(passed)
        self.assertEqual(detail["result"], "no_result")

    def test_unsupported_stream_never_emits_model_text(self):
        from kng.retrieval import Context

        class BadLLM(self.UnsupportedJudge):
            calls = failures = 0

            def complete_stream(self, *args, **kwargs):
                yield "The moon is cheese [1]."

            def complete_json(self, *args, **kwargs):
                if kwargs.get("name") == "validate_evidence":
                    return {"verdict": "sufficient", "reason": ""}
                return super().complete_json(*args, **kwargs)

        ctx = Context(
            question="What did Jagan say about SECI?",
            passages=[{
                "chunk_id": "c1", "source_file": "f.pdf", "citation": "f.pdf p.1",
                "text": "SECI offered a tariff.", "source_type": "source_doc",
                "ranks": {"vector": 1, "keyword": 1},
            }],
            entities=[{"matched": "SECI", "name": "SECI"}],
        )
        with mock.patch("kng.providers.get_llm", return_value=BadLLM()):
            events = list(synthesize.stream_answer(ctx.question, ctx=ctx))
        self.assertNotIn("delta", [kind for kind, _ in events])
        final = events[-1][1]
        self.assertTrue(final.refused)
        self.assertNotIn("moon", final.text.lower())

    def test_partial_provider_stream_is_never_accepted(self):
        from kng.retrieval import Context

        class BrokenLLM:
            model = "broken"
            calls = failures = 0

            def complete_json(self, *args, **kwargs):
                return {"verdict": "sufficient", "reason": ""}

            def complete_stream(self, *args, **kwargs):
                yield "A seemingly complete claim [1]."
                raise RuntimeError("connection lost")

        ctx = Context(
            question="What did Jagan say about SECI?",
            passages=[{
                "chunk_id": "c1", "source_file": "f.pdf", "citation": "f.pdf p.1",
                "text": "A seemingly complete claim.", "source_type": "source_doc",
                "ranks": {"vector": 1, "keyword": 1},
            }],
            entities=[{"matched": "SECI", "name": "SECI"}],
        )
        with mock.patch("kng.providers.get_llm", return_value=BrokenLLM()):
            events = list(synthesize.stream_answer(ctx.question, ctx=ctx))
        self.assertIn("error", [kind for kind, _ in events])
        self.assertNotIn("delta", [kind for kind, _ in events])
        self.assertTrue(events[-1][1].refused)
        self.assertIn("ended before completion", events[-1][1].refusal_reason)


class TestSourceRendering(unittest.TestCase):
    def test_clip_keeps_whole_words(self):
        self.assertTrue(synthesize._clip("alpha beta gamma", 8).endswith("…"))
        self.assertEqual(synthesize._clip("alpha  beta", 40), "alpha beta")
        window = synthesize._evidence_window(
            ("before " * 100) + "exact quote" + (" after" * 100),
            "exact quote", 120)
        self.assertIn("exact quote", window)
        self.assertLessEqual(len(window), 125)

    def test_passages_and_facts_share_one_numbering(self):
        from kng.retrieval import Context
        ctx = Context(
            question="q",
            passages=[{"citation": "fileA p.1", "text": "passage text"}],
            facts=[{"source": "A", "source_type": "Person", "relation": "ACCUSES",
                    "target": "B", "target_type": "Party", "weight": 3,
                    "press_meet_ids": ["10"], "first_date": "2024-01-01",
                    "evidence": [{"quote": "q", "citation": "fileB p.2"}]}])
        sources = synthesize.build_sources(ctx)
        self.assertEqual([s["n"] for s in sources], [1, 2])
        self.assertEqual(sources[1]["kind"], "fact")
        self.assertIn("ACCUSES", sources[1]["text"])

    def test_prompt_namespace_contains_only_model_visible_sources(self):
        passages = [
            {"n": i, "kind": "passage", "citation": f"p{i}", "text": "x" * 100,
             "source_file": f"f{i}", "chunk_id": f"c{i}"}
            for i in range(1, 21)
        ]
        facts = [{
            "n": 21, "kind": "fact", "citation": "fact", "text": "A --R--> B",
            "source_file": "fact.pdf", "chunk_id": "fact-1",
        }]
        selected = synthesize.select_prompt_sources(passages + facts, budget=3000)
        self.assertLess(len(selected), len(passages) + len(facts))
        self.assertEqual([s["n"] for s in selected], list(range(1, len(selected) + 1)))
        self.assertTrue(any(s["kind"] == "fact" for s in selected))

    def test_graph_quote_promotes_its_exact_underlying_chunk(self):
        from kng.retrieval import Context
        from kng.store import graph as gstore

        ctx = Context(
            question="SECI tariff",
            facts=[{
                "source": "Jagan", "source_type": "Person",
                "relation": "MAKES_CLAIM", "target": "₹2.49 tariff",
                "target_type": "Claim", "structural": False, "relevance": 1.0,
                "evidence": [{
                    "chunk_id": "c1", "source_file": "data/release.docx",
                    "citation": "release.docx", "quote": "₹2.49 per unit",
                }],
            }],
        )
        record = {
            "source_file": "data/release.docx", "citation": "release.docx",
            "text": "Jagan said the agreement was ₹2.49 per unit.",
            "source_type": "press_release", "press_meet_id": "10",
        }
        with mock.patch.object(gstore, "chunk_record", return_value=record):
            sources = synthesize.build_sources(ctx)
        promoted = [s for s in sources if s.get("graph_promoted")]
        self.assertEqual(len(promoted), 1)
        self.assertEqual(promoted[0]["chunk_id"], "c1")
        self.assertIn("₹2.49", promoted[0]["text"])


class TestGraphFilterScope(unittest.TestCase):
    def test_filter_is_applied_before_evidence_cap(self):
        edge = {
            "source": "A", "source_type": "Person", "relation": "ACCUSES",
            "target": "B", "target_type": "Party", "weight": 5,
            "press_meet_ids": ["1", "10"], "evidence": [
                {
                    "chunk_id": f"c{i}", "source_file": f"f{i}.pdf",
                    "citation": f"f{i}", "press_meet_id": "1",
                } for i in range(4)
            ] + [{
                "chunk_id": "c10", "source_file": "f10.pdf",
                "citation": "f10", "press_meet_id": "10",
            }],
        }
        fact = gctx._scope_fact(
            gctx._fact(edge, {"10"}), hybrid.Filters(press_meet_id="10"))
        self.assertIsNotNone(fact)
        self.assertEqual(fact["press_meet_ids"], ["10"])
        self.assertEqual(fact["evidence"][0]["chunk_id"], "c10")


class TestEntityLinking(unittest.TestCase):
    def _graph(self):
        import networkx as nx
        G = nx.MultiDiGraph()
        G.add_node("Person:jagan mohan reddy", name="Y. S. Jagan Mohan Reddy",
                   type="Person", aliases=["Jagan"], mention_count=100,
                   press_meet_ids=["10"])
        G.add_node("Organization:ttd", name="TTD", type="Organization",
                   aliases=[], mention_count=20, press_meet_ids=["10"])
        G.add_node("PressMeet:10", name="Press meet 10", type="PressMeet",
                   aliases=[], mention_count=1, press_meet_ids=["10"])
        return G

    def test_links_alias_and_ignores_question_words(self):
        hits = gctx.link_entities(self._graph(), "What did Jagan say about TTD?")
        names = {h["name"] for h in hits}
        self.assertEqual(names, {"Y. S. Jagan Mohan Reddy", "TTD"})

    def test_longest_span_wins(self):
        hits = gctx.link_entities(self._graph(), "Y. S. Jagan Mohan Reddy on TTD")
        self.assertEqual(hits[0]["matched"], "y s jagan mohan reddy")

    def test_no_match_returns_empty(self):
        self.assertEqual(gctx.link_entities(self._graph(), "who is where"), [])

    def test_relevance_scores_topic_overlap(self):
        def fact(target, quote="", structural=False, in_meet=False):
            return {"source": "Y. S. Jagan Mohan Reddy", "relation": "MAKES_CLAIM",
                    "target": target, "evidence": [{"quote": quote}],
                    "structural": structural, "in_retrieved_meet": in_meet}

        terms = {"tirupati", "laddu", "adulteration"}
        on_topic = gctx._relevance(fact("Laddu adulteration probe"), terms)
        off_topic = gctx._relevance(fact("Free electricity for farmers"), terms)
        self.assertGreater(on_topic, 0.3)
        self.assertLessEqual(off_topic, 0.3)

    def test_relevance_ignores_the_entity_that_was_linked(self):
        # "jagan" is in every edge of the Jagan node, so it must not be part of
        # the term set that decides which of his edges are on topic.
        f = {"source": "Y. S. Jagan Mohan Reddy", "relation": "MAKES_CLAIM",
             "target": "Free electricity", "evidence": [],
             "structural": False, "in_retrieved_meet": False}
        with_name = gctx._relevance(f, {"jagan", "laddu"})
        without_name = gctx._relevance(f, {"laddu"})
        self.assertGreater(with_name, without_name)
        self.assertLessEqual(without_name, 0.3)

    def test_fact_rank_prefers_asserted_edges_in_retrieved_meets(self):
        asserted = {"hop": 1, "in_retrieved_meet": True, "structural": False,
                    "relation": "MAKES_CLAIM", "weight": 2}
        structural = {"hop": 1, "in_retrieved_meet": True, "structural": True,
                      "relation": "MENTIONS", "weight": 99}
        far = {"hop": 2, "in_retrieved_meet": True, "structural": False,
               "relation": "MAKES_CLAIM", "weight": 99}
        ranked = sorted([structural, far, asserted], key=gctx._fact_rank)
        self.assertEqual(ranked, [asserted, structural, far])

    def test_direct_claim_beats_high_weight_membership_at_equal_relevance(self):
        claim = {"hop": 1, "relevance": 0.75, "structural": False,
                 "relation": "MAKES_CLAIM", "weight": 1}
        membership = {"hop": 1, "relevance": 0.75, "structural": False,
                      "relation": "MEMBER_OF", "weight": 100}
        self.assertLess(gctx._fact_rank(claim), gctx._fact_rank(membership))


if __name__ == "__main__":
    unittest.main()
