#!/usr/bin/env python3
"""Golden CLI contract tests: exit codes, stdout/stderr shapes, and JSON keys."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE_ROOT = Path(__file__).resolve().parent.parent


class GoldenCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root, ignore=shutil.ignore_patterns(".git", ".mt", "examples", ".DS_Store"))
        for card in (self.root / "cards").glob("*.md"):
            card.unlink()
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text(encoding="utf-8").replace("demo_workspace: true", "demo_workspace: false"),
                          encoding="utf-8")
        self.env = {**os.environ, "MT_USERNAME": "golden-user", "NO_COLOR": "1"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def mt(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    def test_golden_lifecycle_human_and_json_shapes(self) -> None:
        added = self.mt("add", "Write golden tests", "--project", "core", "--priority", "high", "--json")
        self.assertEqual(added.returncode, 0, added.stderr)
        add_data = json.loads(added.stdout)
        self.assertTrue({"ref", "path", "content_hash", "changed"}.issubset(add_data))
        ref = add_data["ref"]
        self.assertTrue(ref.startswith("@"))

        note = self.mt("note", "Architecture note", "--title", "Arch Note", "--json")
        self.assertEqual(note.returncode, 0, note.stderr)
        note_ref = json.loads(note.stdout)["ref"]

        listed = self.mt("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(f"[{ref}] [task] [inbox] [high] Write golden tests", listed.stdout)
        self.assertIn(f"[{note_ref}] [note] [none] Arch Note", listed.stdout)

        searched = self.mt("search", "golden", "--json")
        self.assertEqual(searched.returncode, 0, searched.stderr)
        search_items = json.loads(searched.stdout)
        self.assertEqual(len(search_items), 1)
        self.assertEqual(search_items[0]["ref"], ref)

        focused = self.mt("focus")
        self.assertEqual(focused.returncode, 0, focused.stderr)
        self.assertIn(ref, focused.stdout)
        self.assertIn("Write golden tests", focused.stdout)

        moved = self.mt("move", ref, "active")
        self.assertEqual(moved.returncode, 0, moved.stderr)
        self.assertIn("Moved to active and validated:", moved.stdout)

        commented = self.mt("comment", ref, "-m", "Looks good")
        self.assertEqual(commented.returncode, 0, commented.stderr)
        self.assertIn("Commented and validated:", commented.stdout)

        done = self.mt("done", ref, "--json")
        self.assertEqual(done.returncode, 0, done.stderr)
        done_data = json.loads(done.stdout)
        self.assertEqual(done_data["ref"], ref)
        self.assertTrue(done_data["changed"])

        validated = self.mt("validate")
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertIn("OK: validated 1 task file(s), 1 note file(s), and 0 draft file(s).", validated.stdout)

    def test_golden_error_exit_codes_and_messages(self) -> None:
        missing = self.mt("move", "@deadbeef", "active")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no task matches", missing.stderr.lower())

        empty_search = self.mt("search", "   ")
        self.assertEqual(empty_search.returncode, 2)
        self.assertIn("error: search requires a non-empty query", empty_search.stderr)

        bad_edit = self.mt("edit", "@deadbeef")
        self.assertEqual(bad_edit.returncode, 2)
        self.assertIn("edit requires", bad_edit.stderr)


if __name__ == "__main__":
    unittest.main()
