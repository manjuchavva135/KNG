"""WP4 unit tests — the pure logic, run with no model, no index and no network.

    python -m unittest discover -s tests -v

`unittest` rather than pytest so the suite needs no dependency the pipeline does
not already have. Everything here is deterministic: fusion order, citation
verification, filter escaping and entity linking are exactly the parts where a
silent regression would corrupt an answer's provenance rather than crash.
"""
from __future__ import annotations

import unittest

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


class TestSourceRendering(unittest.TestCase):
    def test_clip_keeps_whole_words(self):
        self.assertTrue(synthesize._clip("alpha beta gamma", 8).endswith("…"))
        self.assertEqual(synthesize._clip("alpha  beta", 40), "alpha beta")

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
        asserted = {"hop": 1, "in_retrieved_meet": True, "structural": False, "weight": 2}
        structural = {"hop": 1, "in_retrieved_meet": True, "structural": True, "weight": 99}
        far = {"hop": 2, "in_retrieved_meet": True, "structural": False, "weight": 99}
        ranked = sorted([structural, far, asserted], key=gctx._fact_rank)
        self.assertEqual(ranked, [asserted, structural, far])


if __name__ == "__main__":
    unittest.main()
