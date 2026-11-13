from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import card_query
from card_fakes import FakeCard
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.web_queries import collection_results


class WebCollectionQueryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 3, 28, 12, tzinfo=ZoneInfo("Europe/London"))
        self.cards = [
            self.card("today", due="2026-03-28", tags=["Research"], rank=1),
            self.card("past", status="doing", due="2026-03-27T12:00:00", rank=0),
            self.card("note", "note", body="Research notes", updated="2026-03-29"),
            self.card("hidden", hidden=True, status="doing", due="2026-03-30"),
            self.card("archived", archived=True, due="2026-03-29"),
            self.card("done-a", status="done", completed="2026-01-01", updated="2026-12-01"),
            self.card("done-b", status="done", completed="2026-01-02"),
        ]

    def card(self, identity, kind="task", **values):
        return FakeCard(identity, kind, **values)

    def results(self, done_cutoff=None, **query):
        return collection_results(
            self.cards, card_query.query_from_mapping(query, today=self.now.date()), now=self.now,
            done_cutoff=done_cutoff, open_statuses=["active", "doing", "waiting"],
            display_rank=lambda card: (card.frontmatter.get("rank", 0), card.path.name))

    def shown(self, **query):
        result = self.results(**query)
        return result["tasks"] + result["notes"]

    def test_done_cutoff_leaves_old_done_tasks_out_of_tasks_but_not_the_archive(self):
        self.cards.append(self.card("done-new", status="done", completed="2026-03-20T09:00:00"))
        self.cards.append(self.card("done-undated", status="done"))
        result = self.results(done_cutoff="2026-03-14")
        self.assertEqual(result["tasks"][-2:], ["done-new", "done-undated"])
        self.assertNotIn("done-a", result["tasks"])
        self.assertNotIn("done-b", result["tasks"])
        self.assertTrue({"done-a", "done-b", "done-new", "done-undated"} <= set(result["archive"]))

    def test_text_covers_title_id_tags_next_action_and_body_and_a_tag_filter_matches_whole_tags(self):
        for field in ("title", "id", "next_action", "body"):
            with self.subTest(field=field):
                card = self.card("a", **{field: "Mixed NEEDLE Text"})
                self.cards = [card]
                self.assertEqual(self.shown(text=" needle "), [card.frontmatter["id"]])
        self.setUp()
        self.assertEqual(self.shown(text="RESEARCH"), ["today", "note"])  # a tag on today, the body of note
        self.assertEqual(self.shown(tag="research"), ["today"])
        self.assertEqual(self.shown(tag="sear"), [])

    def test_filters_combine_and_notes_and_archive_ignore_status(self):
        self.cards[0].frontmatter.update(project="Project one", topic="Work")
        self.assertEqual(self.shown(project="Project one", topic="Work", priority="medium",
                                    type="task", status="active"), ["today"])
        self.assertEqual(self.shown(project="PROJECT ONE"), ["today"])
        self.assertEqual(self.shown(project="Project one", priority="high"), [])
        self.assertEqual(self.shown(status="doing", type="note"), ["note"])
        self.assertEqual(self.results(status="doing")["archive"], ["done-b", "done-a", "archived"])

    def test_topic_filter_includes_descendants_case_insensitively_on_segment_boundaries(self):
        for identity, topic in (("today", "Learning"), ("past", "learning/rust/async"), ("note", "learners")):
            next(card for card in self.cards if card.frontmatter["id"] == identity).frontmatter["topic"] = topic
        self.assertEqual(self.shown(topic="learning"), ["past", "today"])
        self.assertEqual(self.shown(topic="LEARNING/rust"), ["past"])
        self.assertEqual(self.shown(topic="learn"), [])

    def test_a_facet_matches_any_selected_value_and_facets_combine(self):
        self.cards[0].frontmatter.update(project="Alpha", topic="learning/rust", tags=["a"])
        self.cards[1].frontmatter.update(project="beta", topic="work", tags=["b"], priority="high")
        self.assertEqual(self.shown(status=["active", "doing"]), ["past", "today", "note"])
        self.assertEqual(self.shown(project=["ALPHA", "Beta"]), ["past", "today"])
        self.assertEqual(self.shown(topic=["learning", "work"]), ["past", "today"])
        self.assertEqual(self.shown(topic=["learning", "garden"]), ["today"])
        self.assertEqual(self.shown(tag=["A", "b"]), ["past", "today"])
        self.assertEqual(self.shown(tag=["a", "b"], priority=["high"]), ["past"])
        self.assertEqual(self.shown(type=["note", "task"], status=["doing"]), ["past", "note"])

    def test_a_single_string_is_one_value_for_every_list_field(self):
        self.assertEqual(card_query.query_from_mapping({"status": "doing", "tag": " x ", "due": "", "topic": ""}),
                         card_query.query_from_mapping({"status": ["doing"], "tag": ["x"]}))
        self.assertEqual(self.shown(status="doing"), ["past", "note"])

    def test_due_selection_matches_any_selected_option(self):
        self.assertEqual(set(self.shown(due=["overdue", "today"])), {"past", "today"})
        self.assertEqual(set(self.shown(due=["today", "none"])), {"today", "done-b", "done-a", "note"})

    def test_due_boundaries_include_end_of_day_and_calendar_days_across_dst(self):
        self.assertEqual(self.shown(due="overdue"), ["past"])
        self.assertEqual(self.shown(due="today"), ["today"])
        self.cards += [self.card("next7", due="2026-04-04"), self.card("next8", due="2026-04-05")]
        self.assertEqual(set(self.shown(due="week")), {"today", "next7"})
        self.assertEqual(set(self.shown(due="next7")), {"today", "next7"})
        self.assertEqual(self.shown(due="none"), ["done-b", "done-a", "note"])
        self.cards.append(self.card("offset", due="2026-03-28T12:30:00+01:00"))
        self.assertIn("offset", self.shown(due="overdue"))

    def test_updated_range_keeps_cards_within_or_older_than_a_span(self):
        self.cards += [self.card("recent", updated="2026-03-20T09:00:00"), self.card("stale", updated="2025-12-01")]
        since = lambda span: set(self.shown(updated_since=span))
        self.assertEqual(since("2w"), {"recent", "done-a", "note"})
        self.assertEqual(since("3m"), {"recent", "done-a", "note"})
        self.assertEqual(since("1y"), {"recent", "done-a", "note", "stale"})
        self.assertEqual(set(self.shown(updated_before="3m")), {"stale"})  # a card without `updated` matches no range
        self.assertEqual(set(self.shown(updated_before="1y")), set())
        with self.assertRaises(card_query.QueryError):
            self.results(updated_since="bogus")

    def test_visibility_and_focus_scopes_and_deadlines_preserve_current_behavior(self):
        result = self.results()
        self.assertNotIn("hidden", result["tasks"])
        self.assertIn("hidden", self.results(hidden="include")["tasks"])
        self.assertNotIn("hidden", self.results(hidden="include")["open"])
        self.assertNotIn("done-b", result["open"])
        self.assertEqual(result["deadlines"], ["past", "today", "hidden"])
        self.cards[0].frontmatter.update(project="Launch")
        self.assertEqual(self.results(project="launch")["deadlines"], ["today"])
        self.assertEqual(self.results(text="no matches")["deadlines"], [])
        # Deadlines hold only dated cards, whatever due mode the board filter asks for.
        self.assertEqual(self.results(due="none")["deadlines"], [])
        self.assertEqual(self.results(due=["none", "today"])["deadlines"], ["today"])
        self.assertEqual(self.results(due="any")["deadlines"], ["past", "today", "hidden"])
        self.assertIn("archived", result["archive"])
        self.assertNotIn("archived", result["tasks"])

    def test_orders_all_results_with_stable_ties_without_mutating_cards(self):
        self.cards += [self.card("note-b", "note", updated="2026-03-29"),
                       self.card("done-c", status="done", completed="2026-01-02")]
        before = deepcopy([card.frontmatter for card in self.cards])
        result = self.results()
        self.assertEqual(result["tasks"], ["past", "today", "done-c", "done-b", "done-a"])
        self.assertEqual(result["notes"], ["note-b", "note"])
        self.assertEqual([card.frontmatter for card in self.cards], before)

    def test_review_only_applies_to_every_collection_and_no_implicit_limit(self):
        for index in (0, 2, 4):
            self.cards[index].frontmatter["needs_review"] = True
        self.cards.append(self.card("reviewed", "note"))
        result = self.results(needs_review=True)
        self.assertEqual(self.shown(needs_review=True), ["today", "note"])
        self.assertEqual(result["archive"], ["archived"])
        self.cards += [self.card(f"task-{index}") for index in range(120)]
        self.assertEqual(len(self.shown()), 126)

    def test_archive_collection_holds_both_kinds_by_recency(self):
        self.cards.append(self.card("old-note", "note", archived=True, updated="2026-02-01"))
        result = self.results()
        self.assertEqual(result["archive"], ["old-note", "done-b", "done-a", "archived"])
        self.assertEqual(result["archive_notes"], ["old-note"])
        self.assertEqual(result["archive_tasks"], ["done-b", "done-a", "archived"])
        self.assertEqual(set(result), {"tasks", "notes", "open", "archive", "archive_tasks",
                                       "archive_notes", "deadlines"})

    def test_a_kind_filter_empties_the_other_kinds_collections(self):
        self.assertEqual(self.results(kind="note")["tasks"], [])
        self.assertEqual(self.results(kind="task")["notes"], [])


if __name__ == "__main__":
    unittest.main()
