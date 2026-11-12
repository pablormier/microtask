from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402


class DemoWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "repo"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"),
        )
        self.env = os.environ.copy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
            env=env or self.env,
        )

    def test_demo_mode_active_on_empty_workspace_with_examples(self) -> None:
        result = self.run_mt("list", "--status", "active")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 3)
        self.assertIn("Plan quarterly roadmap", result.stdout)
        self.assertIn("Demo mode:", result.stderr)

    def test_demo_mode_search_and_read(self) -> None:
        searched = self.run_mt("search", "roadmap")
        self.assertEqual(searched.returncode, 0, searched.stderr)
        self.assertIn("Plan quarterly roadmap", searched.stdout)

        read = self.run_mt("read", "Plan quarterly roadmap")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(self.run_mt("read", "Plan quarterly roadmap", "--json").stdout)["properties"]["title"], "Plan quarterly roadmap")

    def test_demo_mode_date_rebasing(self) -> None:
        read = self.run_mt("read", "Follow up with design agency")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn("due:", read.stdout)
        raw_example = (SOURCE_ROOT / "examples" / "cards" / "2026-01-03-090000-follow-up-with-design-agency.md").read_text(encoding="utf-8")
        self.assertIn("2026-01-08", raw_example)

    def test_demo_has_resolved_task_and_note_reference_trails(self) -> None:
        result = self.run_mt("list", "--all", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        items = {item["title"]: item for item in json.loads(result.stdout)}
        for title in ("Plan quarterly roadmap", "microtask workflow cheatsheet", "Set up shared team inbox"):
            with self.subTest(title=title):
                references = items[title]["references"]
                self.assertEqual(len(references), 2)
                self.assertTrue(all(ref["state"] == "resolved" for ref in references))
                self.assertEqual({ref["target"]["title"] for ref in references},
                    {"Plan quarterly roadmap", "microtask workflow cheatsheet", "Set up shared team inbox"} - {title})
        active = self.run_mt("list", "--status", "active", "--json")
        self.assertEqual(active.returncode, 0, active.stderr)
        roadmap = next(item for item in json.loads(active.stdout) if item["title"] == "Plan quarterly roadmap")
        self.assertEqual({reference["target"]["title"] for reference in roadmap["references"]},
                         {"microtask workflow cheatsheet", "Set up shared team inbox"})

    def test_creating_task_transitions_out_of_demo_mode(self) -> None:
        # Verify demo mode before creation
        listed_before = self.run_mt("list", "--status", "active")
        self.assertEqual(len(listed_before.stdout.splitlines()), 3)

        # Create real task
        added = self.run_mt("add", "My real production task")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertNotIn("Demo mode:", added.stderr)

        # Verify real task is in inbox and demo mode is deactivated
        inbox = self.run_mt("list", "--status", "inbox")
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        self.assertIn("My real production task", inbox.stdout)
        self.assertNotIn("Demo mode:", inbox.stderr)

        # Active tasks should now be 0 (demo tasks deactivated)
        active = self.run_mt("list", "--status", "active")
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(active.stdout, "")

    def test_creating_note_transitions_out_of_demo_mode(self) -> None:
        created = self.run_mt("note", "My real production note")
        self.assertEqual(created.returncode, 0, created.stderr)

        # Demo notes should be replaced by the single real note
        notes_folder = list(card_files(self.root, "note"))
        self.assertEqual(len(notes_folder), 1)

    def test_demo_mode_mutations(self) -> None:
        # Move a task in demo mode
        moved = self.run_mt("move", "Plan quarterly roadmap", "doing")
        self.assertEqual(moved.returncode, 0, moved.stderr)

        doing = self.run_mt("list", "--status", "doing")
        self.assertIn("Plan quarterly roadmap", doing.stdout)

        # Complete a task in demo mode
        done = self.run_mt("done", "Plan quarterly roadmap")
        self.assertEqual(done.returncode, 0, done.stderr)

        # Delete a task in demo mode
        deleted = self.run_mt("delete", "Organize desk cables")
        self.assertEqual(deleted.returncode, 0, deleted.stderr)

    def test_demo_workspace_disabled_by_policy(self) -> None:
        policy_local = self.root / "mt-policy.local.yaml"
        policy_local.write_text("rules:\n  demo_workspace: false\n", encoding="utf-8")

        result = self.run_mt("list", "--status", "active")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Demo mode:", result.stderr)

        info = self.run_mt("info")
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn("0 cards", info.stdout)


if __name__ == "__main__":
    unittest.main()
