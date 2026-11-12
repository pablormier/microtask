from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
import card_format  # noqa: E402
import tasklib  # noqa: E402


class TopicPathTests(unittest.TestCase):
    def test_path_validation_and_normalization(self) -> None:
        for good in ("work", "learning/rust/async", "a b/c"):
            self.assertIsNone(card_format.topic_path_error(good), good)
        for bad in ("", "  ", "a//b", "/a", "a/", "a/ /b", "a\nb"):
            self.assertIsNotNone(card_format.topic_path_error(bad), repr(bad))
        self.assertEqual(card_format.normalize_topic("  learning / rust  /async "), "learning/rust/async")
        properties = {"id": "x", "title": "T", "kind": "note", "priority": "none",
                      "created": "2026-01-01", "updated": "2026-01-01"}
        self.assertEqual(card_format.validate_properties({**properties, "topic": "a/b"}), [])
        self.assertTrue(card_format.validate_properties({**properties, "topic": "a//b"}))
        self.assertTrue(card_format.validate_properties({**properties, "topic": "a / b"}))
        self.assertTrue(card_format.validate_properties({**properties, "topic": ["a"]}))
        self.assertNotIn("area", card_format.EDITABLE_METADATA)

    def test_descendant_matching_uses_whole_segments_and_ignores_case(self) -> None:
        self.assertTrue(card_format.topic_within("learning", "learning"))
        self.assertTrue(card_format.topic_within("Learning/Rust/Async", "learning/rust"))
        self.assertFalse(card_format.topic_within("learning", "learn"))
        self.assertFalse(card_format.topic_within("learning/rust", "learning/rus"))
        self.assertFalse(card_format.topic_within("learning", "learning/rust"))
        self.assertFalse(card_format.topic_within(None, "learning"))

    def test_tree_implies_parents_counts_descendants_and_adds_suggestions(self) -> None:
        tree = card_format.topic_tree({"learning/rust/async": 2, "learning": 1, "Learning/go": 1}, ["home"])
        self.assertEqual([node["depth"] for node in tree], [0, 0, 1, 1, 2])
        by_path = {node["path"].lower(): node["count"] for node in tree}
        self.assertEqual(by_path, {"home": 0, "learning": 4, "learning/go": 1,
                                   "learning/rust": 2, "learning/rust/async": 2})


class TopicCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root, ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"))
        for card in (self.root / "cards").glob("*.md"):
            card.unlink()
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text().replace("demo_workspace: true", "demo_workspace: false"))

    def mt(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def ok(self, *args: str) -> str:
        result = self.mt(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def add(self, title: str, topic: str | None = None) -> str:
        arguments = ["add", "--kind", "note", "--title", title, "--json"]
        if topic is not None:
            arguments += ["--topic", topic]
        return json.loads(self.ok(*arguments))["ref"]

    def topics(self) -> dict[str, int]:
        tree = json.loads(self.ok("topic", "list", "--json"))["topics"]
        return {node["path"]: node["count"] for node in tree}

    def card_topics(self) -> dict[str, object]:
        return {item["title"]: item.get("topic")
                for item in json.loads(self.ok("list", "--all", "--json"))}

    def test_set_normalizes_and_rejects_invalid_paths(self) -> None:
        ref = self.add("One", "learning / rust")
        self.assertEqual(json.loads(self.ok("read", ref, "--json"))["properties"]["topic"], "learning/rust")
        for bad in ("a//b", "/a", "a/"):
            self.assertNotEqual(self.mt("edit", ref, "--topic", bad).returncode, 0, bad)
        self.assertEqual(json.loads(self.ok("read", ref, "--json"))["properties"]["topic"], "learning/rust")
        self.assertNotEqual(self.mt("add", "x", "--area", "work").returncode, 0)

    def test_list_filter_includes_descendants_on_segment_boundaries(self) -> None:
        self.add("Top", "learning")
        self.add("Deep", "learning/rust/async")
        self.add("Other", "learners")
        self.add("None")
        def titles(topic: str) -> list[str]:
            return sorted(item["title"] for item in json.loads(self.ok("list", "--topic", topic, "--all", "--json")))
        self.assertEqual(titles("learning"), ["Deep", "Top"])
        self.assertEqual(titles("LEARNING/Rust"), ["Deep"])
        self.assertEqual(titles("learn"), [])
        search = json.loads(self.ok("search", "Deep", "--topic", "learning", "--all", "--json"))
        self.assertEqual([item["title"] for item in search], ["Deep"])

    def test_topic_list_shows_tree_with_implied_parents_and_suggestions(self) -> None:
        self.add("A", "learning/rust/async")
        self.add("B", "learning")
        counts = self.topics()
        self.assertEqual(counts["learning"], 2)
        self.assertEqual(counts["learning/rust"], 1)
        self.assertEqual(counts["learning/rust/async"], 1)
        self.assertEqual(counts["work"], 0)  # suggested_topics seeds empty top-level topics
        text = self.ok("topic", "list")
        self.assertIn("learning/ (2)\n", text)
        self.assertNotIn("rust", text)

    def nested(self) -> None:
        for title, topic in (("A", "learning/papers/causality"), ("B", "learning/papers/causality"),
                             ("C", "learning/papers/optimization"), ("D", "learning/rust"),
                             ("E", "work/decider"), ("F", "work")):
            self.add(title, topic)

    def test_topic_list_one_level_with_slash_markers(self) -> None:
        self.nested()
        roots = self.ok("topic", "list").splitlines()
        self.assertEqual([line for line in roots if "/" in line or "(0)" not in line],
                         ["learning/ (4)", "work/ (2)"])
        self.assertIn("personal (0)", roots)  # suggested_topics seed empty roots
        self.assertEqual(self.ok("topic", "list", "learning"), "papers/ (3)\nrust (1)\n")
        self.assertEqual(self.ok("topic", "list", "learning/"), "papers/ (3)\nrust (1)\n")  # as printed above
        self.assertEqual(self.ok("topic", "list", "LEARNING/Papers"), "causality (2)\noptimization (1)\n")
        self.assertEqual(self.ok("topic", "list", "learning/rust"), "")

    def test_topic_list_unknown_topic_and_suggested_topic(self) -> None:
        self.nested()
        result = self.mt("topic", "list", "nope")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr.strip(), "error: unknown topic: nope")
        self.assertEqual(self.ok("topic", "list", "personal"), "")  # suggested_topics counts as known

    def test_topic_list_tree_draws_connectors(self) -> None:
        self.nested()
        lines = self.ok("topic", "list", "--tree").splitlines()
        start = lines.index("learning (4)")
        self.assertEqual(lines[start:start + 5], [
            "learning (4)", "├── papers (3)", "│   ├── causality (2)",
            "│   └── optimization (1)", "└── rust (1)"])
        self.assertIn("work (2)\n└── decider (1)\n", self.ok("topic", "list", "--tree"))
        self.assertEqual(self.ok("topic", "list", "--tree", "learning/papers"),
                         "learning/papers (3)\n├── causality (2)\n└── optimization (1)\n")

    def test_topic_list_paths_in_tree_order(self) -> None:
        self.nested()
        paths = self.ok("topic", "list", "--paths").splitlines()
        self.assertEqual(paths[paths.index("learning"):paths.index("learning") + 5], [
            "learning", "learning/papers", "learning/papers/causality",
            "learning/papers/optimization", "learning/rust"])
        self.assertEqual(self.ok("topic", "list", "--paths", "learning/papers"),
                         "learning/papers\nlearning/papers/causality\nlearning/papers/optimization\n")

    def test_topic_list_json_with_topic_and_exclusive_formats(self) -> None:
        self.nested()
        subtree = json.loads(self.ok("topic", "list", "--json", "learning/papers"))["topics"]
        self.assertEqual([(node["path"], node["name"], node["depth"], node["count"], node["own"]) for node in subtree], [
            ("learning/papers", "papers", 1, 3, 0), ("learning/papers/causality", "causality", 2, 2, 2),
            ("learning/papers/optimization", "optimization", 2, 1, 1)])
        self.assertGreater(len(json.loads(self.ok("topic", "list", "--json"))["topics"]), len(subtree))
        for pair in (("--tree", "--paths"), ("--tree", "--json"), ("--paths", "--json")):
            self.assertEqual(self.mt("topic", "list", *pair).returncode, 2, pair)

    def test_topic_list_has_no_color_codes_when_piped(self) -> None:
        self.nested()
        for arguments in ((), ("--tree",)):
            self.assertNotIn("\033", self.ok("topic", "list", *arguments))

    def test_validate_warns_when_casing_differs_but_still_passes(self) -> None:
        self.add("A", "Learning/Rust")
        self.add("B", "learning/rust")
        result = self.mt("validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING", result.stderr)
        self.assertIn("letter case", result.stderr)

    def test_rename_dry_run_then_subtree_move(self) -> None:
        self.add("Root", "learning")
        self.add("Child", "learning/rust")
        self.add("Deep", "learning/rust/async")
        self.add("Elsewhere", "work")
        before = (self.root / "cards").glob("*.md")
        snapshot = {path: path.read_bytes() for path in before}
        preview = self.ok("topic", "rename", "learning", "study", "--dry-run")
        self.assertIn("Would move 3 card(s)", preview)
        self.assertEqual({path: path.read_bytes() for path in (self.root / "cards").glob("*.md")}, snapshot)
        self.ok("topic", "rename", "Learning/Rust", "study/rust")  # case-insensitive source, subtree
        self.assertEqual(self.card_topics(), {"Root": "learning", "Child": "study/rust",
                                              "Deep": "study/rust/async", "Elsewhere": "work"})
        self.assertEqual(self.mt("validate").returncode, 0)

    def test_rename_refuses_existing_target_unless_merge_and_own_subtree(self) -> None:
        self.add("A", "notes/rust")
        self.add("B", "learning/rust")
        refused = self.mt("topic", "rename", "learning", "notes")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--merge", refused.stderr)
        self.assertEqual(self.card_topics()["B"], "learning/rust")
        subtree = self.mt("topic", "rename", "learning", "learning/old")
        self.assertEqual(subtree.returncode, 1)
        self.assertIn("own subtree", subtree.stderr)
        self.assertNotEqual(self.mt("topic", "rename", "nothing", "x").returncode, 0)
        self.assertNotEqual(self.mt("topic", "rename", "learning", "a//b").returncode, 0)
        self.ok("topic", "rename", "learning", "notes", "--merge")
        self.assertEqual(self.card_topics(), {"A": "notes/rust", "B": "notes/rust"})

    def test_rename_only_recases_and_moves_up_without_merge_flag(self) -> None:
        self.add("A", "learning/rust")
        self.ok("topic", "rename", "learning", "Learning")
        self.assertEqual(self.card_topics()["A"], "Learning/rust")
        self.ok("topic", "rename", "Learning/rust", "rust")
        self.assertEqual(self.card_topics()["A"], "rust")

    def test_rename_refuses_claimed_tasks_before_writing_unless_by_matches(self) -> None:
        self.add("Note", "learning")
        self.ok("add", "--kind", "task", "--status", "doing", "--by", "other-agent", "--title", "Claimed",
                "--topic", "learning/rust")
        snapshot = {path: path.read_bytes() for path in (self.root / "cards").glob("*.md")}
        refused = self.mt("topic", "rename", "learning", "study", "--by", "me")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("other-agent", refused.stderr)
        self.assertEqual({path: path.read_bytes() for path in (self.root / "cards").glob("*.md")}, snapshot)
        self.ok("topic", "rename", "learning", "study", "--by", "other-agent")
        self.assertEqual(self.card_topics(), {"Note": "study", "Claimed": "study/rust"})

    def test_validate_warns_about_unmigrated_area_and_policy_key(self) -> None:
        ref = self.add("Old", "work")
        path = next((self.root / "cards").glob("*.md"))
        path.write_text(path.read_text().replace("topic: work", "area: work"))
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text().replace("suggested_topics:", "suggested_areas:"))
        result = self.mt("validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(path.name, result.stderr)
        self.assertIn("suggested_areas", result.stderr)
        self.assertIn("migrate_area_to_topic.py --plan", result.stderr)
        plan = json.loads(subprocess.run([sys.executable, str(self.root / "scripts" / "migrate_area_to_topic.py"), "--plan"],
                                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout)
        self.assertTrue(plan["policy"]["suggested_areas_renamed"])

    def test_rename_rolls_back_every_card_when_validation_fails(self) -> None:
        self.add("A", "learning")
        self.add("B", "learning/rust")
        cards = sorted((self.root / "cards").glob("*.md"))
        before = {path: path.read_bytes() for path in cards}
        arguments = argparse.Namespace(old="learning", new="study", dry_run=False, merge=False, as_json=False)
        with patch.object(tasklib, "REPO_ROOT", self.root), patch.object(tasklib, "_DEMO_STATE", False), \
             patch.object(tasklib, "validate_data_root", side_effect=ValueError("forced validation failure")):
            self.assertEqual(tasklib.command_topic_rename(arguments), 1)
        self.assertEqual({path: path.read_bytes() for path in cards}, before)

    def test_rename_detects_concurrent_change(self) -> None:
        self.add("A", "learning")
        cards = sorted((self.root / "cards").glob("*.md"))
        arguments = argparse.Namespace(old="learning", new="study", dry_run=False, merge=False, as_json=False)
        real = tasklib.apply_item_transaction

        def concurrent(changes, **kwargs):
            cards[0].write_bytes(cards[0].read_bytes() + b"\nedited meanwhile\n")
            return real(changes, **kwargs)

        with patch.object(tasklib, "REPO_ROOT", self.root), patch.object(tasklib, "_DEMO_STATE", False), \
             patch.object(tasklib, "apply_item_transaction", side_effect=concurrent):
            self.assertEqual(tasklib.command_topic_rename(arguments), 1)
        self.assertIn("edited meanwhile", cards[0].read_text())
        self.assertIn("topic: learning", cards[0].read_text())


class AreaMigrationTests(unittest.TestCase):
    def test_migration_renames_area_in_cards_drafts_and_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            shutil.copytree(SOURCE_ROOT, root, ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"))
            for card in (root / "cards").glob("*.md"):
                card.unlink()
            policy = root / "mt-policy.yaml"
            policy.write_text(policy.read_text().replace("demo_workspace: true", "demo_workspace: false")
                              .replace("suggested_topics:", "suggested_areas:"))
            run = lambda *a: subprocess.run([str(root / "mt"), *a], cwd=root, capture_output=True, text=True,
                                                   stdin=subprocess.DEVNULL)
            ref = json.loads(run("add", "--kind", "note", "--title", "Old", "--project", "p", "--json").stdout)["ref"]
            path = next((root / "cards").glob("*.md"))
            text = path.read_text()
            path.write_text(text.replace("project: p\n", "project: p\narea: work\n", 1))
            (root / "drafts").mkdir(exist_ok=True)
            created = run("draft", "create", "note", "--title", "D", "--json")
            self.assertEqual(created.returncode, 0, created.stderr)
            draft = root / "drafts" / f"{json.loads(created.stdout)['id']}.md"
            draft.write_text(draft.read_text().replace("\ntitle:", "\narea: home\ntitle:", 1))
            script = root / "scripts" / "migrate_area_to_topic.py"
            plan = subprocess.run([sys.executable, str(script), "--plan"], capture_output=True, text=True)
            self.assertIn(path.name, plan.stdout)
            self.assertIn("area: work", path.read_text())  # plan does not write
            applied = subprocess.run([sys.executable, str(script), "--apply"], capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            migrated = path.read_text()
            self.assertIn("topic: work", migrated)
            self.assertNotIn("area:", migrated)
            self.assertIn("topic: home", draft.read_text())
            self.assertIn("suggested_topics:", policy.read_text())
            checked = run("validate")
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(run("list", "--topic", "work", "--json").stdout)[0]["ref"], ref)
            again = subprocess.run([sys.executable, str(script), "--apply"], capture_output=True, text=True)
            self.assertEqual(again.returncode, 0, again.stderr)


if __name__ == "__main__":
    unittest.main()
