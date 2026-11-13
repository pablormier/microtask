"""One card query: unit rules for matching and parsing, and guards that every consumer shares it."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import card_query  # noqa: E402
import tasklib  # noqa: E402
from card_fakes import FakeCard  # noqa: E402
from card_query import Query, QueryError  # noqa: E402

NOW = datetime(2026, 3, 28, 12)
STATUSES = ["inbox", "active", "doing", "waiting", "done"]
PRIORITIES = ["low", "medium", "high"]


def ids(cards):
    return [card.frontmatter["id"] for card in cards]


def selected(cards, **query):
    return ids(card_query.select(cards, card_query.query_from_mapping(query, today=NOW.date()), now=NOW))


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.cards = [
            FakeCard("a", project="Alpha", topic="Work/Infra", tags=["Rust"], type="Bug", priority="high",
                     created="2026-03-01T09:00:00", updated="2026-03-20T09:00:00", due="2026-03-28"),
            FakeCard("b", status="doing", project="beta", topic="home", tags=["x", "y"], priority="low"),
            FakeCard("c", "note", title="Plan notes", body="Remember the milk", topic="work",
                     updated="2026-03-27T10:00:00", needs_review=True),
            FakeCard("d", status="inbox", due="2026-03-20T08:00:00"),
            FakeCard("e", status="done", completed="2026-03-10T10:00:00"),
        ]

    def test_each_list_field_matches_any_value_and_fields_combine(self):
        self.assertEqual(selected(self.cards, kind="note"), ["c"])
        self.assertEqual(selected(self.cards, status=["doing", "inbox"]), ["b", "d"])
        self.assertEqual(selected(self.cards, priority=["high", "low"]), ["a", "b"])
        self.assertEqual(selected(self.cards, type="bug"), ["a"])
        self.assertEqual(selected(self.cards, project=["ALPHA", "Beta"]), ["a", "b"])
        self.assertEqual(selected(self.cards, topic="work"), ["a", "c"])
        self.assertEqual(selected(self.cards, topic=["work/infra", "home"]), ["a", "b"])
        self.assertEqual(selected(self.cards, topic="wor"), [])
        self.assertEqual(selected(self.cards, tag=["rust", "y"]), ["a", "b"])
        self.assertEqual(selected(self.cards, tag="rus"), [])
        self.assertEqual(selected(self.cards, project="alpha", priority="low"), [])

    def test_status_priority_and_type_select_tasks_only(self):
        self.cards[2].frontmatter.update(status="active", priority="high", type="Bug")
        for field, value in (("status", "active"), ("priority", "high"), ("type", "bug")):
            self.assertNotIn("c", selected(self.cards, **{field: value}))

    def test_review_and_triage(self):
        self.assertEqual(selected(self.cards, needs_review=True), ["c"])
        self.assertEqual(selected(self.cards, triage=True), ["c", "d"])
        self.cards[0].frontmatter["needs_review"] = True
        self.assertEqual(selected(self.cards, triage=True), ["a", "c", "d"])

    def test_date_ranges_use_the_day_and_a_missing_date_never_matches(self):
        self.assertEqual(selected(self.cards, created_since="2026-03-01"), ["a"])
        self.assertEqual(selected(self.cards, created_before="2026-03-01"), [])
        self.assertEqual(selected(self.cards, updated_since="2026-03-20", updated_before="2026-03-27"), ["a"])
        self.assertEqual(selected(self.cards, updated_since="2026-03-27"), ["c"])
        self.assertEqual(selected(self.cards, completed_since="2026-03-10", completed_before="2026-03-11"), ["e"])
        self.assertEqual(selected(self.cards, due_since="2026-03-28"), ["a"])
        self.assertEqual(selected(self.cards, due_before="2026-03-28"), ["d"])
        self.assertEqual(selected(self.cards, updated_since="2w"), ["a", "c"])  # 2026-03-14 on

    def test_due_modes_at_their_boundaries(self):
        cards = [FakeCard("end", due="2026-03-28"), FakeCard("past", due="2026-03-28T11:59:59"),
                 FakeCard("later", due="2026-03-28T12:00:01"), FakeCard("week", due="2026-04-04"),
                 FakeCard("beyond", due="2026-04-05"), FakeCard("none")]
        self.assertEqual(selected(cards, due="overdue"), ["past"])
        self.assertEqual(selected(cards, due="today"), ["end", "past", "later"])
        self.assertEqual(selected(cards, due="week"), ["end", "past", "later", "week"])
        self.assertEqual(selected(cards, due="none"), ["none"])
        self.assertEqual(selected(cards, due="any"), ["end", "past", "later", "week", "beyond"])
        self.assertEqual(selected(cards, due=["overdue", "none"]), ["past", "none"])
        self.assertEqual(card_query.query_from_mapping({"due": "any"}).due, ("any",))

    def test_visibility_has_three_states_for_archived_and_hidden(self):
        cards = [FakeCard("plain"), FakeCard("arch", archived=True), FakeCard("hid", hidden=True),
                 FakeCard("both", archived=True, hidden=True)]
        self.assertEqual(selected(cards), ["plain"])
        self.assertEqual(selected(cards, archived="include"), ["plain", "arch"])
        # Only archived means every archived card, hidden or not, unless hidden is given.
        self.assertEqual(selected(cards, archived="only"), ["arch", "both"])
        self.assertEqual(selected(cards, archived="only", hidden="exclude"), ["arch"])
        self.assertEqual(selected(cards, hidden="include"), ["plain", "hid"])
        self.assertEqual(selected(cards, hidden="only"), ["hid", "both"])
        only = card_query.query_from_mapping({"archived": "only", "hidden": "exclude"})
        self.assertEqual(card_query.query_from_mapping(card_query.query_to_mapping(only)), only)
        self.assertEqual(selected(cards, archived="include", hidden="include"), ids(cards))
        self.assertEqual(selected(cards, archived="only", hidden="only"), ["both"])

    def test_text_matches_title_id_and_body_in_each_mode(self):
        self.assertEqual(selected(self.cards, text="remember the"), ["c"])
        self.assertEqual(selected(self.cards, text="the remember"), [])
        self.assertEqual(selected(self.cards, text="the remember", match="and"), ["c"])
        self.assertEqual(selected(self.cards, text="milk zebra", match="and"), [])
        self.assertEqual(selected(self.cards, text="milk zebra", match="or"), ["c"])
        self.assertEqual(selected(self.cards, text="rememmber milk", match="fuzzy"), ["c"])
        self.assertEqual(selected(self.cards, text="PLAN"), ["c"])
        self.assertEqual(selected(self.cards, text="rust"), ["a"])  # tags

    def test_text_matches_the_short_reference(self):
        card = FakeCard("ref-card")
        card.short_ref = "3af71006"
        other = FakeCard("other")
        other.short_ref = "93af7100"
        cards = [card, other]
        for mode in card_query.MATCH_MODES:
            self.assertEqual(selected(cards, text="@3af7", match=mode), ["ref-card"], mode)
        self.assertEqual(selected(cards, text="@3AF"), ["ref-card"])  # a prefix, not a substring
        self.assertEqual(selected(cards, text="@af7"), [])
        self.assertIsNone(card_query.text_score(card, "@4", "exact"))

    def test_select_drops_old_done_tasks_only_when_given_a_cutoff(self):
        query = Query()
        self.assertEqual(ids(card_query.select(self.cards, query, now=NOW, done_cutoff="2026-03-14")),
                         ["a", "b", "c", "d"])
        undated = [FakeCard("u", status="done"), FakeCard("v", status="done", updated="2026-03-01T00:00:00")]
        self.assertEqual(ids(card_query.select(undated, query, now=NOW, done_cutoff="2026-03-14")), ["u"])

    def test_default_done_cutoff_is_off_when_the_query_asks_for_old_work(self):
        today = date(2026, 3, 28)
        self.assertEqual(card_query.default_done_cutoff(Query(), "2w", today), "2026-03-14")
        for asked in (dict(status=("done",)), dict(archived="include"), dict(archived="only"),
                      dict(completed_since="2026-01-01"), dict(completed_before="2026-01-01")):
            self.assertIsNone(card_query.default_done_cutoff(Query(**asked), "2w", today), asked)
        self.assertEqual(card_query.default_done_cutoff(Query(hidden="include"), "2w", today), "2026-03-14")

    def test_narrow_limits_a_list_field_or_reports_nothing_can_match(self):
        self.assertEqual(card_query.narrow(Query(), "status", ["a", "b"]).status, ("a", "b"))
        self.assertEqual(card_query.narrow(Query(status=("b", "z")), "status", ["a", "b"]).status, ("b",))
        self.assertIsNone(card_query.narrow(Query(status=("z",)), "status", ["a", "b"]))


class FocusDueTests(unittest.TestCase):
    """Focus ranks by the same due rule as `--due`: a date alone is due until the end of that day."""

    def test_focus_and_due_modes_share_the_overdue_today_and_week_boundaries(self):
        now = datetime.now()
        day = lambda offset: (now.date() + timedelta(days=offset)).isoformat()
        cards = [FakeCard("end-today", due=day(0), due_kind="deadline"),
                 FakeCard("yesterday", due=day(-1), due_kind="deadline", priority="high"),
                 FakeCard("earlier-today", due=f"{day(0)}T00:00:00", due_kind="deadline"),
                 FakeCard("tomorrow", due=day(1), due_kind="deadline"),
                 FakeCard("week-edge", due=day(7), due_kind="deadline"),
                 FakeCard("beyond", due=day(8), due_kind="deadline", status="waiting"),
                 FakeCard("no-due")]
        for card in cards:
            card.path = Path(f"{card.frontmatter['id']}.md")
        rank = {"low": 2, "medium": 1, "high": 0}
        reasons = {task.frontmatter["id"]: reason for task, reason in tasklib.ranked_focus_candidates(cards, rank)}

        def due(mode):
            return set(ids(card_query.select(cards, Query(due=(mode,)), now=now)))

        due_today = {name for name, reason in reasons.items() if reason == "due_today"}
        self.assertEqual(due_today, due("today") - due("overdue"))
        self.assertEqual({name for name, reason in reasons.items() if reason in ("high_overdue", "deadline_review")},
                         due("overdue") - {"no-due"})
        self.assertEqual({name for name, reason in reasons.items() if reason == "due_soon"},
                         due("week") - due("today"))
        self.assertEqual(reasons["end-today"], "due_today")
        self.assertEqual(reasons["yesterday"], "high_overdue")
        self.assertEqual(reasons["week-edge"], "due_soon")


class ParsingTests(unittest.TestCase):
    def test_round_trip_through_the_canonical_mapping(self):
        query = card_query.query_from_mapping({
            "kind": ["task"], "status": ["doing", "active"], "topic": " Work / infra ", "tag": "x",
            "needs_review": "true", "updated_since": "2w", "due_before": "2026-05-01", "due": ["overdue", "none"],
            "archived": "only", "text": "two words", "match": "and"}, today=date(2026, 3, 28))
        mapping = card_query.query_to_mapping(query)
        self.assertEqual(json.loads(json.dumps(mapping)), mapping)
        self.assertEqual(card_query.query_from_mapping(mapping), query)
        self.assertEqual(mapping["updated_since"], "2026-03-14")
        self.assertEqual(mapping["topic"], ["Work/infra"])
        self.assertEqual(card_query.query_to_mapping(Query()), {})
        self.assertEqual(card_query.query_from_mapping({}), Query())

    def test_aliases_apply_when_parsing_only(self):
        parse = lambda **values: card_query.query_from_mapping(values, today=date(2026, 3, 28))
        self.assertEqual(parse(since="2w").updated_since, "2026-03-14")
        self.assertEqual(parse(due_after="2026-03-01").due_since, "2026-03-01")
        self.assertEqual(parse(task_type="bug").type, ("bug",))
        self.assertEqual(parse(due="next").due, ("overdue", "week"))
        self.assertEqual(parse(due=["next7", "today"]).due, ("week", "today"))
        everything = parse(all=True)
        self.assertEqual((everything.archived, everything.hidden), ("include", "include"))
        self.assertEqual(parse(include_archived=True).archived, "include")
        self.assertEqual(parse(include_hidden="true").hidden, "include")
        self.assertEqual(parse(include_hidden=False).hidden, "exclude")
        self.assertEqual(replace(Query(), due=["next"]).due, ("next",))  # matching never reads aliases

    def test_old_dashboard_addresses_parse_to_the_canonical_query(self):
        values = {"q": ["milk"], "review": ["1"], "hidden": ["1"], "updated": ["older-3m"], "due": ["next7"]}
        query = card_query.query_from_mapping(values, today=date(2026, 3, 28))
        self.assertEqual((query.text, query.needs_review, query.hidden, query.updated_before, query.due),
                         ("milk", True, "include", "2025-12-28", ("week",)))
        self.assertEqual(card_query.query_from_mapping({"updated": ["2w"]}, today=date(2026, 3, 28)).updated_since,
                         "2026-03-14")
        self.assertEqual(card_query.query_from_mapping({"updated": ["all"]}), Query())
        self.assertTrue(card_query.uses_aliases(values))
        self.assertFalse(card_query.uses_aliases({"text": ["milk"], "hidden": ["include"], "due": ["week"]}))

    def test_parse_errors_name_the_flag(self):
        for values, flag in (({"updated_since": "soon"}, "--updated-since"), ({"bogus": "1"}, "bogus"),
                             ({"needs_review": "maybe"}, "--needs-review")):
            with self.assertRaises(QueryError) as caught:
                card_query.query_from_mapping(values)
            self.assertIn(flag, str(caught.exception))

    def test_validate_rejects_impossible_queries(self):
        check = lambda **fields: card_query.validate(Query(**fields), statuses=STATUSES, priorities=PRIORITIES)
        check(status=("doing",), priority=("high",), kind=("task", "note"))
        for fields, flag in ((dict(status=("nope",)), "--status"), (dict(priority=("urgent",)), "--priority"),
                             (dict(kind=("note",), status=("doing",)), "--status"),
                             (dict(kind=("note",), priority=("low",)), "--priority"),
                             (dict(kind=("note",), type=("bug",)), "--type"),
                             (dict(kind=("page",)), "--kind"),
                             (dict(updated_since="2026-03-02", updated_before="2026-03-02"), "--updated-since"),
                             (dict(due=("soon",)), "--due"), (dict(archived="yes"), "--archived"),
                             (dict(hidden="maybe"), "--hidden"), (dict(match="loose"), "--match")):
            with self.subTest(fields=fields), self.assertRaises(QueryError) as caught:
                check(**fields)
            self.assertIn(flag, str(caught.exception))

    def test_query_from_args_applies_env_scope_defaults_and_aliases(self):
        parser = argparse.ArgumentParser()
        card_query.add_query_arguments(parser, defaults={"archived": "include"}, text=True)
        args = parser.parse_args(["--since", "1w", "--task-type", "bug", "--include-hidden", "--and", "milk", "tea"])
        query = card_query.query_from_args(args, env={}, today=date(2026, 3, 28))
        self.assertEqual((query.updated_since, query.type, query.hidden, query.archived),
                         ("2026-03-21", ("bug",), "include", "include"))
        self.assertEqual((query.text, query.match), ("milk tea", "and"))
        scoped = parser.parse_args([])
        self.assertEqual(card_query.query_from_args(scoped, env={"MT_PROJECT": "p", "MT_TOPIC": "a / b"}).topic, ("a/b",))
        self.assertEqual(card_query.env_scope(scoped, {"MT_PROJECT": "p"}), ["project p (MT_PROJECT)"])
        explicit = parser.parse_args(["--project", "mine"])
        self.assertEqual(card_query.query_from_args(explicit, env={"MT_PROJECT": "p"}).project, ("mine",))
        self.assertEqual(card_query.env_scope(explicit, {"MT_PROJECT": "p"}), [])
        self.assertEqual(card_query.query_from_args(parser.parse_args(["--archived", "words"]), env={}).text, "words")
        self.assertEqual(card_query.query_from_args(parser.parse_args(["--hidden=include"]), env={}).hidden, "include")
        self.assertEqual(card_query.query_from_args(parser.parse_args(["--hidden"]), env={}).hidden, "only")


QUERY_FLAGS = [f"--{field.replace('_', '-')}" for field in card_query.LIST_FIELDS + card_query.DATE_FIELDS
               + ("needs_review", "triage", "archived", "hidden")]
COMMANDS = (("list",), ("search",), ("focus",), ("view",), ("bulk-edit",), ("followup",),
            ("time", "list"), ("time", "report"), ("web",))


def subparser(root, path):
    for name in path:
        action = next(action for action in root._actions if isinstance(action, argparse._SubParsersAction))
        root = action.choices[name]
    return root


class RegistrationTests(unittest.TestCase):
    def test_every_selecting_command_accepts_every_canonical_flag(self):
        root = tasklib.parser()
        for path in COMMANDS:
            options = set(subparser(root, path)._option_string_actions)
            text_flags = ["--match", "--and", "--or", "--fuzzy"] if path[0] in ("search", "view") else []
            for flag in QUERY_FLAGS + ["--all", "--include-archived", "--include-hidden", "--since",
                                       "--due-after", "--task-type"] + text_flags:
                with self.subTest(command=" ".join(path), flag=flag):
                    self.assertIn(flag, options)

    def test_old_registries_and_matchers_are_gone(self):
        for name in ("metadata_matcher", "add_card_selection_arguments", "add_metadata_filter_arguments",
                     "select_card_items", "filter_view_items", "view_selector_arguments", "apply_env_scope",
                     "WEB_FILTERS", "WEB_UPDATED_CHOICES", "done_cutoff"):
            self.assertFalse(hasattr(tasklib, name), name)


class StructureTests(unittest.TestCase):
    def test_card_filtering_lives_only_in_card_query(self):
        sources = [path for folder in ("scripts", "tools") for path in sorted((SOURCE_ROOT / folder).glob("*.py"))
                   if path.name not in ("card_query.py", "card_format.py")]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("topic_within(", text)
                self.assertIsNone(re.search(r"""["'](overdue|next7)["']""", text))
        web_queries = (SOURCE_ROOT / "tools" / "web_queries.py").read_text(encoding="utf-8")
        self.assertNotRegex(web_queries, r"def (matches|normalize_query|due_matches)\b")


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root,
                        ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"))

    def mt(self, *args, env=None, check=True):
        result = subprocess.run([str(self.root / "mt"), *args], cwd=self.root, env=env, text=True,
                                capture_output=True, check=False)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def environment(self, **extra):
        import os
        return {**os.environ, **extra}

    def add(self, title, *flags):
        self.mt("add", title, *flags)

    def test_cards_resolve_by_tag_and_list_by_any_due_date(self):
        self.add("Plain card")
        self.add("Dated card", "--due", "2030-01-01", "--due-kind", "target", "--tag", "zebra-tag")
        self.assertIn("Dated card", self.mt("read", "zebra-tag").stdout)
        self.assertIn("Dated card", self.mt("read", "zebra-tag", "--json").stdout)
        listed = self.mt("list", "--due", "any").stdout
        self.assertIn("Dated card", listed)
        self.assertNotIn("Plain card", listed)
        self.assertIn("Plain card", self.mt("list", "--due", "none").stdout)

    def test_list_and_the_dashboard_select_the_same_tasks(self):
        today = date.today()
        self.add("Alpha overdue", "--project", "Alpha", "--due", (today - timedelta(days=2)).isoformat(),
                 "--due-kind", "deadline", "--priority", "high", "--tag", "rust")
        self.add("Alpha soon", "--project", "alpha", "--topic", "work/infra", "--due",
                 (today + timedelta(days=3)).isoformat(), "--due-kind", "target", "--status", "active")
        self.add("Beta later", "--project", "Beta", "--due", (today + timedelta(days=30)).isoformat(),
                 "--due-kind", "target", "--tag", "Rust", "--status", "waiting")
        self.add("Hidden one", "--hidden", "--status", "doing")
        self.add("Archived one", "--archived")
        self.add("Review me", "--needs-review")
        self.add("Finished")
        self.mt("done", "Finished")
        self.mt("note", "A note", "--project", "Alpha")
        queries = [{}, {"project": ["alpha"]}, {"tag": ["RUST"]}, {"status": ["active", "waiting"]},
                   {"due": ["overdue"]}, {"due": ["week"]}, {"due": ["none"]}, {"due_before": (today + timedelta(days=10)).isoformat()},
                   {"hidden": "include"}, {"archived": "only"}, {"archived": "include", "hidden": "include"},
                   {"needs_review": "true"}, {"triage": "true"}, {"topic": ["work"]}, {"priority": ["high"]},
                   {"updated_since": "1w"}, {"updated_before": "1w"}, {"status": ["done"]}, {"completed_since": "1d"}]
        probe = """
import json, sys
sys.path.insert(0, 'tools')
import taskweb
out = []
for mapping in json.loads(sys.argv[1]):
    payload = taskweb.dashboard_payload(query=taskweb.request_query(mapping))
    out.append(payload['collections']['tasks'])
print(json.dumps(out))
"""
        dashboard = json.loads(subprocess.run(
            [sys.executable, "-c", probe, json.dumps(queries)], cwd=self.root, text=True,
            capture_output=True, check=True).stdout)
        for mapping, shown in zip(queries, dashboard):
            flags = []
            for field, value in mapping.items():
                for item in value if isinstance(value, list) else [value]:
                    flags += [f"--{field.replace('_', '-')}"] + ([] if item == "true" else [item])
            listed = [card["id"] for card in json.loads(self.mt("list", "--kind", "task", "--json", *flags).stdout)]
            with self.subTest(query=mapping):
                self.assertEqual(sorted(listed), sorted(shown))

    def test_project_scope_limits_followup_and_time_report(self):
        self.add("Wait on alpha", "--project", "alpha", "--status", "waiting")
        self.add("Wait on beta", "--project", "beta", "--status", "waiting")
        self.mt("time", "add", "Wait on alpha", "30m")
        self.mt("time", "add", "Wait on beta", "15m")
        scoped = self.environment(MT_PROJECT="alpha")
        followup = self.mt("followup", env=scoped)
        self.assertIn("Wait on alpha", followup.stdout)
        self.assertNotIn("Wait on beta", followup.stdout)
        self.assertIn("Scope: project alpha (MT_PROJECT)", followup.stderr)
        report = self.mt("time", "report", env=scoped)
        self.assertIn("Total: 30m", report.stdout)
        self.assertIn("Wait on beta", self.mt("followup", "--project", "beta", env=scoped).stdout)
        self.assertIn("Total: 45m", self.mt("time", "report").stdout)

    def test_time_reports_include_archived_work_and_count_only_sessions_in_the_window(self):
        self.add("Old work")
        self.add("Recent work")
        today = date.today()
        old, recent = today - timedelta(days=40), today - timedelta(days=2)
        self.mt("time", "add", "Old work", "--start", f"{old.isoformat()}T09:00", "--end", f"{old.isoformat()}T10:00")
        self.mt("time", "add", "Recent work", "--start", f"{recent.isoformat()}T09:00",
                "--end", f"{recent.isoformat()}T09:30")
        self.mt("time", "add", "Old work", "--start", f"{recent.isoformat()}T11:00", "--end", f"{recent.isoformat()}T11:15")
        self.mt("edit", "Old work", "--set", "archived=true")
        self.assertIn("Total: 1h 45m", self.mt("time", "report").stdout)
        self.assertIn("Total: 45m", self.mt("time", "report", "--session-since", "1w").stdout)
        self.assertIn("Total: 1h", self.mt("time", "report", "--session-before", "1w").stdout)
        self.assertIn("Old work: 15m", self.mt("time", "report", "--session-since", "2w").stdout)
        self.assertNotIn("Old work", self.mt("time", "report", "--archived", "exclude").stdout)
        listed = self.mt("time", "list", "--session-since", "1w").stdout
        self.assertEqual(len(listed.strip().splitlines()), 2)
        self.assertIn("No tracked time", self.mt("time", "report", "--session-since", "1d").stdout)
        self.assertEqual(self.mt("time", "report", "--session-since", "soon", check=False).returncode, 2)

    def test_old_bulk_edit_plans_ask_for_a_new_preview(self):
        self.add("Plan me", "--status", "active")
        plan = json.loads(self.mt("bulk-edit", "--status", "active", "--set", "priority=high", "--json").stdout)
        self.assertEqual(plan["query"], {"status": ["active"]})
        old = {key: value for key, value in plan.items() if key != "query"} | {"selection": {}}
        path = Path(self.temporary.name) / "old.json"
        path.write_text(json.dumps(old))
        result = self.mt("bulk-edit", "--apply", str(path), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("older mt; run the preview again", result.stderr)


if __name__ == "__main__":
    unittest.main()
