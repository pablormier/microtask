from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402
import support  # noqa: E402


class DemoWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "repo"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", ".DS_Store"),
        )
        self.env = os.environ.copy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return support.run_cli(self.root, *args, env=env or self.env)

    def test_demo_mode_active_on_empty_workspace_with_examples(self) -> None:
        result = self.run_mt("list", "--status", "active")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 5)
        self.assertIn("Prepare the lab meeting readout", result.stdout)
        self.assertIn("Demo mode:", result.stderr)

    def test_demo_mode_search_and_read(self) -> None:
        searched = self.run_mt("search", "readout")
        self.assertEqual(searched.returncode, 0, searched.stderr)
        self.assertIn("Prepare the lab meeting readout", searched.stdout)

        read = self.run_mt("read", "Prepare the lab meeting readout")
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertEqual(json.loads(self.run_mt("read", "Prepare the lab meeting readout", "--json").stdout)["properties"]["title"], "Prepare the lab meeting readout")

    def test_demo_mode_date_rebasing(self) -> None:
        expected = {
            "Re-run the benchmark baseline": (-1, "deadline"),
            "Audit the benchmark split": (0, "deadline"),
            "Check back on ambiguous benchmark cases": (1, "reminder"),
            "Prepare the lab meeting readout": (3, "deadline"),
            "Work through the causal inference DAG exercises": (5, "target"),
        }
        today = date.today()
        for title, (offset, kind) in expected.items():
            with self.subTest(title=title):
                read = self.run_mt("read", title, "--json")
                self.assertEqual(read.returncode, 0, read.stderr)
                properties = json.loads(read.stdout)["properties"]
                self.assertEqual(properties["due"], (today + timedelta(days=offset)).isoformat())
                self.assertEqual(properties["due_kind"], kind)

        title = "Audit the benchmark split"
        edited = self.run_mt("edit", title, "--set", "due=2030-01-05")
        self.assertEqual(edited.returncode, 0, edited.stderr)
        reopened = self.run_mt("read", title, "--json")
        self.assertEqual(json.loads(reopened.stdout)["properties"]["due"], "2030-01-05")

    def test_demo_seeds_example_attachments_unchanged(self) -> None:
        validated = self.run_mt("validate")
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertNotIn("broken attachment link", validated.stdout + validated.stderr)
        examples = self.root / "examples" / "attachments"
        sources = sorted(path for path in examples.iterdir() if not path.name.startswith("."))
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(attachment=source.name):
                seeded = self.root / ".mt" / "demo" / "attachments" / source.name
                self.assertEqual(seeded.read_bytes(), source.read_bytes())

    def test_demo_keeps_edits_until_examples_change_then_rebuilds(self) -> None:
        demo = self.root / ".mt" / "demo"
        moved = self.run_mt("move", "Prepare the lab meeting readout", "doing")
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assertIn("Prepare the lab meeting readout", self.run_mt("list", "--status", "doing").stdout)

        # Unchanged examples keep the demo, edits included.
        self.assertIn("Prepare the lab meeting readout", self.run_mt("list", "--status", "doing").stdout)

        # Changed examples (as after `mt update`) rebuild it: edits are dropped, new files appear.
        examples = self.root / "examples"
        (examples / "attachments" / "0123456789ab-new-figure.svg").write_text("<svg/>", encoding="utf-8")
        listed = self.run_mt("list", "--status", "doing")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertNotIn("Prepare the lab meeting readout", listed.stdout)
        self.assertTrue((demo / "attachments" / "0123456789ab-new-figure.svg").is_file())
        self.assertEqual([path.name for path in (self.root / ".mt").iterdir() if path.name.startswith("demo-")], [])

        # A demo built before fingerprints existed is rebuilt once.
        self.run_mt("move", "Prepare the lab meeting readout", "doing")
        (demo / ".examples-sha256").unlink()
        self.assertNotIn("Prepare the lab meeting readout", self.run_mt("list", "--status", "doing").stdout)
        self.assertTrue((demo / ".examples-sha256").is_file())

    def test_demo_has_resolved_task_and_note_reference_trails(self) -> None:
        result = self.run_mt("list", "--all", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        items = {item["title"]: item for item in json.loads(result.stdout)}
        for title, item in items.items():
            with self.subTest(title=title):
                self.assertTrue(item["references"])
                self.assertTrue(all(ref["state"] == "resolved" for ref in item["references"]))

        for title, destination in (
            ("Audit the benchmark split", "Benchmark split protocol"),
            ("Causal inference course tracker", "Work through the causal inference DAG exercises"),
            ("Does a longer protein chain cause higher prediction error?", "Investigate the noisy long-chain slice"),
        ):
            self.assertIn(destination, {ref["target"]["title"] for ref in items[title]["references"]})

    def test_creating_task_transitions_out_of_demo_mode(self) -> None:
        # Verify demo mode before creation
        listed_before = self.run_mt("list", "--status", "active")
        self.assertEqual(len(listed_before.stdout.splitlines()), 5)

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
        moved = self.run_mt("move", "Prepare the lab meeting readout", "doing")
        self.assertEqual(moved.returncode, 0, moved.stderr)

        doing = self.run_mt("list", "--status", "doing")
        self.assertIn("Prepare the lab meeting readout", doing.stdout)

        # Complete a task in demo mode
        done = self.run_mt("done", "Prepare the lab meeting readout")
        self.assertEqual(done.returncode, 0, done.stderr)

        # Delete a task in demo mode
        deleted = self.run_mt("delete", "Re-run the benchmark baseline")
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
