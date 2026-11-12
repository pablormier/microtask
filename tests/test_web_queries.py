from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tools.web_queries import collection_results, matches, normalize_query


class WebCollectionQueryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 3, 28, 12, tzinfo=ZoneInfo("Europe/London"))
        self.cards = [
            self.card("today", due="2026-03-28", tags=["Research"], display_rank=[1]),
            self.card("past", status="doing", due="2026-03-27T12:00:00", display_rank=[0]),
            self.card("note", kind="note", type="", body="Research notes", updated="2026-03-29"),
            self.card("hidden", hidden=True, status="doing", due="2026-03-30"),
            self.card("archived", archived=True, due="2026-03-29"),
            self.card("done-a", status="done", completed="2026-01-01", updated="2026-12-01"),
            self.card("done-b", status="done", completed="2026-01-02"),
        ]

    def card(self, identity, **values):
        return dict(id=identity, kind="task", status="active", priority="medium", type="task",
                    title=identity, path=identity, tags=[]) | values

    def results(self, **query):
        return collection_results(self.cards, query, now=self.now)

    def test_done_since_leaves_old_done_tasks_out_of_cards_but_not_the_archive(self):
        self.cards.append(self.card("done-new", status="done", completed="2026-03-20T09:00:00"))
        self.cards.append(self.card("done-undated", status="done"))
        result = collection_results(self.cards, {}, now=self.now, done_since="2026-03-14")
        self.assertEqual(result["cards"][-3:], ["done-new", "done-undated", "note"])
        self.assertNotIn("done-a", result["cards"])
        self.assertNotIn("done-b", result["cards"])
        self.assertTrue({"done-a", "done-b", "done-new", "done-undated"} <= set(result["archive"]))

    def test_search_covers_body_refs_ids_next_actions_and_tags_case_insensitively(self):
        for field in ("title", "ref", "id", "next_action", "body"):
            with self.subTest(field=field):
                item = self.card("a", **{field: "Mixed NEEDLE Text"})
                self.assertTrue(matches(item, normalize_query({"search": " needle "}), self.now))
        result = self.results(search="RESEARCH")
        self.assertEqual(result["cards"], ["today", "note"])
        self.assertEqual(self.results(tag="research")["cards"], ["today"])
        self.assertEqual(self.results(tag="sear")["cards"], [])

    def test_filters_combine_and_notes_and_archive_ignore_status(self):
        self.cards[0].update(project="Project one", topic="Work")
        self.assertEqual(self.results(project="Project one", topic="Work", priority="medium",
                                      type="task", status="active")["cards"], ["today"])
        self.assertEqual(self.results(project="PROJECT ONE")["cards"], ["today"])
        self.assertEqual(self.results(project="Project one", priority="high")["cards"], [])
        result = self.results(status="doing", type="note")
        self.assertEqual(result["cards"], ["note"])
        self.assertEqual(self.results(status="doing")["archive"], ["done-b", "done-a", "archived"])

    def test_topic_filter_includes_descendants_case_insensitively_on_segment_boundaries(self):
        for identity, topic in (("today", "Learning"), ("past", "learning/rust/async"), ("note", "learners")):
            next(card for card in self.cards if card["id"] == identity)["topic"] = topic
        self.assertEqual(self.results(topic="learning")["cards"], ["past", "today"])
        self.assertEqual(self.results(topic="LEARNING/rust")["cards"], ["past"])
        self.assertEqual(self.results(topic="learn")["cards"], [])

    def test_a_facet_matches_any_selected_value_and_facets_combine(self):
        self.cards[0].update(project="Alpha", topic="learning/rust", tags=["a"])
        self.cards[1].update(project="beta", topic="work", tags=["b"], priority="high")
        self.assertEqual(self.results(status=["active", "doing"])["cards"], ["past", "today", "note"])
        self.assertEqual(self.results(project=["ALPHA", "Beta"])["cards"], ["past", "today"])
        self.assertEqual(self.results(topic=["learning", "work"])["cards"], ["past", "today"])
        self.assertEqual(self.results(topic=["learning", "garden"])["cards"], ["today"])
        self.assertEqual(self.results(tag=["A", "b"])["cards"], ["past", "today"])
        self.assertEqual(self.results(tag=["a", "b"], priority=["high"])["cards"], ["past"])
        self.assertEqual(self.results(type=["note", "task"], status=["doing"])["cards"], ["past", "note"])

    def test_a_single_string_and_all_still_work_for_every_list_facet(self):
        self.assertEqual(normalize_query({"status": "doing", "tag": " x ", "due": "all", "topic": ""}),
                         normalize_query({"status": ["doing"], "tag": ["x"]}))
        self.assertEqual(normalize_query({"status": "all"})["status"], [])
        self.assertEqual(self.results(status="doing")["cards"], ["past", "note"])

    def test_due_selection_matches_any_selected_option(self):
        self.assertEqual(set(self.results(due=["overdue", "today"])["cards"]), {"past", "today"})
        self.assertEqual(set(self.results(due=["today", "none"])["cards"]), {"today", "done-b", "done-a", "note"})

    def test_due_boundaries_include_end_of_day_and_calendar_days_across_dst(self):
        self.assertEqual(self.results(due="overdue")["cards"], ["past"])
        self.assertEqual(self.results(due="today")["cards"], ["today"])
        self.cards += [self.card("next7", due="2026-04-04"), self.card("next8", due="2026-04-05")]
        self.assertEqual(set(self.results(due="next7")["cards"]), {"today", "next7"})
        self.assertEqual(self.results(due="none")["cards"], ["done-b", "done-a", "note"])
        self.cards.append(self.card("offset", due="2026-03-28T12:30:00+01:00"))
        self.assertIn("offset", self.results(due="overdue")["cards"])

    def test_updated_filter_keeps_cards_within_or_older_than_a_span(self):
        self.assertEqual(self.results(updated="all")["cards"], self.results()["cards"])
        self.cards += [self.card("recent", updated="2026-03-20T09:00:00"), self.card("stale", updated="2025-12-01")]
        cards = lambda span: set(self.results(updated=span)["cards"])
        self.assertEqual(cards("2w"), {"recent", "done-a", "note"})
        self.assertEqual(cards("3m"), {"recent", "done-a", "note"})
        self.assertEqual(cards("1y"), {"recent", "done-a", "note", "stale"})
        self.assertEqual(cards("older-3m"), {"today", "past", "done-b", "stale"})
        self.assertEqual(cards("older-1y"), {"today", "past", "done-b"})
        self.assertEqual(cards("bogus"), cards("all"))

    def test_visibility_and_focus_scopes_and_deadlines_preserve_current_behavior(self):
        result = self.results()
        self.assertNotIn("hidden", result["cards"])
        self.assertIn("hidden", self.results(show_hidden="true")["cards"])
        self.assertNotIn("hidden", self.results(show_hidden=True)["open"])
        self.assertNotIn("done-b", result["open"])
        self.assertEqual(result["deadlines"], ["past", "today", "hidden"])
        self.cards[0].update(project="Launch")
        self.assertEqual(self.results(project="launch")["deadlines"], ["today"])
        self.assertEqual(self.results(search="no matches")["deadlines"], [])
        self.assertIn("archived", result["archive"])
        self.assertNotIn("archived", result["cards"])

    def test_orders_all_results_with_stable_ties_without_mutating_cards(self):
        self.cards += [self.card("note-b", kind="note", updated="2026-03-29"),
                       self.card("done-c", status="done", completed="2026-01-02")]
        before = deepcopy(self.cards)
        result = self.results()
        self.assertEqual(result["cards"][:2], ["past", "today"])
        self.assertEqual(result["cards"][2:5], ["done-c", "done-b", "done-a"])
        self.assertEqual(result["cards"][5:], ["note-b", "note"])  # tasks by rank first, then notes by recency
        self.assertEqual(self.cards, before)

    def test_review_only_applies_to_every_collection_and_no_implicit_limit(self):
        self.cards[2]["needs_review"] = True
        self.cards[0]["needs_review"] = True
        self.cards[4]["needs_review"] = True
        self.cards.append(self.card("reviewed", kind="note"))
        result = self.results(needs_review=True)
        self.assertEqual(result["cards"], ["today", "note"])
        self.assertEqual(result["archive"], ["archived"])
        self.cards += [self.card(f"task-{index}") for index in range(120)]
        self.assertEqual(len(self.results()["cards"]), 126)

    def test_archive_collection_holds_both_kinds_by_recency(self):
        self.cards.append(self.card("old-note", kind="note", archived=True, updated="2026-02-01"))
        self.assertEqual(self.results()["archive"], ["old-note", "done-b", "done-a", "archived"])
        self.assertEqual(set(self.results()), {"cards", "open", "archive", "deadlines"})

if __name__ == "__main__":
    unittest.main()
