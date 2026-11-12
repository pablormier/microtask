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
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
import card_format
import tasklib


class MoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root,
                        ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"))
        self.env = {**os.environ, "MT_USERNAME": "move-test", "MT_AUTO_SYNC": "false"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root, env=self.env,
                              input=input_bytes, capture_output=True, check=False)

    def json(self, *args: str, input_bytes: bytes | None = None) -> dict:
        result = self.run_mt(*args, input_bytes=input_bytes)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout)

    def task(self, title: str, body: str = "Body\n", *, next_action: str | None = "Start here") -> dict:
        args = ["add", "--title", title, "--file", "-", "--json"]
        if next_action is not None:
            args.extend(["--next-action", next_action])
        return self.json(*args, input_bytes=body.encode())

    def note(self, title: str, body: str = "Note body\n") -> dict:
        return self.json("note", "--title", title, "--file", "-", "--json", input_bytes=body.encode())

    def read(self, ref: str) -> dict:
        return self.json("read", ref, "--json")

    def test_moves_preserve_body_and_optional_action_while_claiming_doing(self) -> None:
        created = self.task("Book hotel", "## Waiting\nExact body\n", next_action="Call the hotel")
        original = self.read(created["ref"])
        for status in ("active", "doing", "waiting"):
            moved = self.run_mt("move", created["ref"], status)
            self.assertEqual(moved.returncode, 0, moved.stderr.decode("utf-8", errors="replace"))
            current = self.read(created["ref"])
            self.assertEqual(current["properties"]["status"], status)
            self.assertEqual(current["body"], original["body"])
            self.assertEqual(current["properties"]["next_action"], "Call the hotel")
            if status == "doing":
                self.assertEqual(current["properties"]["claimed_by"], "move-test")
                self.assertTrue(current["properties"]["claimed_at"])
        final = self.read(created["ref"])
        self.assertNotIn("claimed_by", final["properties"])
        self.assertNotIn("claimed_at", final["properties"])

    def test_comments_are_structured_multiline_and_work_on_notes(self) -> None:
        created = self.task("Call clinic")
        message = "Left voicemail\nReception called back"
        result = self.run_mt("comment", created["ref"], "-m", message)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        commented = self.read(created["ref"])
        self.assertEqual(commented["properties"]["comments"][-1]["text"], message)
        self.assertRegex(commented["properties"]["comments"][-1]["at"], r"^\d{4}-\d{2}-\d{2}T")

        note = self.note("Design principle", "Keep the source intact.")
        result = self.run_mt("comment", note["ref"], "-m", "Useful for editor UI")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        saved_note = self.read(note["ref"])
        self.assertEqual(saved_note["properties"]["comments"][-1]["text"], "Useful for editor UI")
        self.assertEqual(saved_note["body"], "Keep the source intact.")

    def test_move_and_completion_messages_become_comment_records(self) -> None:
        task = self.task("Book hotel")
        moved = self.run_mt("move", task["ref"], "--comment", "Waiting on dates", "waiting")
        self.assertEqual(moved.returncode, 0, moved.stderr.decode("utf-8", errors="replace"))
        waiting = self.read(task["ref"])
        self.assertEqual(waiting["properties"]["status"], "waiting")
        self.assertEqual(waiting["properties"]["comments"][-1]["text"], "Waiting on dates")

        done = self.run_mt("done", task["ref"], "--note", "Reservation confirmed",
                           "--comment", "Confirmation saved")
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", errors="replace"))
        completed = self.read(task["ref"])
        self.assertEqual(completed["properties"]["status"], "done")
        texts = [comment["text"] for comment in completed["properties"]["comments"]]
        self.assertIn("Reservation confirmed", texts)
        self.assertIn("Confirmation saved", texts)
        self.assertEqual(completed["body"], "Body\n")

    def test_same_status_is_a_byte_preserving_noop_and_closed_destinations_are_rejected(self) -> None:
        task = self.task("No-op task")
        path = self.root / task["path"]
        before = path.read_bytes()
        noop = self.run_mt("move", task["ref"], "inbox")
        self.assertEqual(noop.returncode, 0, noop.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(path.read_bytes(), before)
        for destination in ("done", "notes", "reference", "unsupported"):
            with self.subTest(destination=destination):
                rejected = self.run_mt("move", task["ref"], destination)
                self.assertEqual(rejected.returncode, 2)
                self.assertIn(b"move destination must be one of", rejected.stderr)
                self.assertEqual(path.read_bytes(), before)

    def test_tasks_without_next_action_can_move_and_wip_limit_rolls_back(self) -> None:
        no_action = self.task("No action", "", next_action=None)
        self.assertNotIn("next_action", self.read(no_action["ref"])["properties"])
        moved = self.run_mt("move", no_action["ref"], "doing")
        self.assertEqual(moved.returncode, 0, moved.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(self.read(no_action["ref"])["properties"]["status"], "doing")
        self.assertEqual(self.run_mt("move", no_action["ref"], "active").returncode, 0)

        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text(encoding="utf-8").replace("max_doing_tasks: 5", "max_doing_tasks: 1"),
                          encoding="utf-8")
        first = self.task("First task")
        second = self.task("Second task")
        self.assertEqual(self.run_mt("move", first["ref"], "doing").returncode, 0)
        blocked = self.run_mt("move", second["ref"], "doing")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertEqual(self.read(first["ref"])["properties"]["status"], "doing")
        self.assertEqual(self.read(second["ref"])["properties"]["status"], "inbox")

    def test_conversion_preserves_id_body_and_comments_without_restoring_removed_fields(self) -> None:
        task = self.task("Keep design note", "## Next action\nLiteral heading.\n",
                         next_action="Start Keep design note")
        self.run_mt("comment", task["ref"], "-m", "Existing task comment")
        original = self.read(task["ref"])
        converted = self.run_mt("convert", task["ref"], "note")
        self.assertEqual(converted.returncode, 0, converted.stderr.decode("utf-8", errors="replace"))
        note = self.read(task["ref"])
        self.assertEqual(note["properties"]["id"], original["properties"]["id"])
        self.assertEqual(note["body"], original["body"])
        self.assertNotIn("status", note["properties"])
        self.assertNotIn("next_action", note["properties"])
        texts = [comment["text"] for comment in note["properties"]["comments"]]
        self.assertIn("Existing task comment", texts)
        self.assertTrue(any("Next action:" in text and "Start Keep design note" in text for text in texts))

        converted_back = self.run_mt("convert", task["ref"], "task")
        self.assertEqual(converted_back.returncode, 0, converted_back.stderr.decode("utf-8", errors="replace"))
        new_task = self.read(task["ref"])
        self.assertEqual(new_task["body"], original["body"])
        self.assertEqual(new_task["properties"]["status"], "inbox")
        self.assertNotIn("next_action", new_task["properties"])
        self.assertEqual(new_task["properties"]["comments"], note["properties"]["comments"])

    def test_conversion_drops_empty_task_time_records_without_mutating_comments(self) -> None:
        created = self.task("Empty time records", "Keep this body.\n", next_action="Review history")
        self.assertEqual(self.run_mt("comment", created["ref"], "-m", "Existing history").returncode, 0)
        path = self.root / created["path"]
        properties, body = card_format.parse_document(path.read_text(encoding="utf-8"), expected_kind="task")
        properties.update(
            time_entries=[], claimed_by=None, claimed_at=None,
            project="research", tags=["keep"], extension={"source": "external"},
        )
        path.write_text(card_format.render_document(properties, body, expected_kind="task"), encoding="utf-8")

        task = tasklib.Card.read(path)
        comments_before = list(task.frontmatter["comments"])
        preview = tasklib.render_converted_note(task, "2026-11-10")
        preview_properties, preview_body = card_format.parse_document(preview, expected_kind="note")
        self.assertFalse(set(card_format.TASK_ONLY_FIELDS) & set(preview_properties))
        self.assertEqual(preview_properties["project"], "research")
        self.assertEqual(preview_properties["tags"], ["keep"])
        self.assertEqual(preview_properties["extension"], {"source": "external"})
        self.assertEqual(preview_body, body)
        self.assertEqual(task.frontmatter["comments"], comments_before)
        self.assertEqual(preview_properties["comments"][:-1], comments_before)
        self.assertIn("Converted from task", preview_properties["comments"][-1]["text"])

        converted = self.run_mt("convert", created["ref"], "note")
        self.assertEqual(converted.returncode, 0, converted.stderr.decode("utf-8", errors="replace"))
        saved = self.read(created["ref"])
        self.assertEqual(saved["properties"]["kind"], "note")
        self.assertNotIn("time_entries", saved["properties"])
        self.assertNotIn("claimed_by", saved["properties"])
        self.assertEqual(saved["properties"]["comments"][:-1], comments_before)
        self.assertEqual(saved["properties"]["extension"], {"source": "external"})
        self.assertEqual(saved["body"], body)

    def test_done_task_cannot_convert_to_note_and_validate_accepts_optional_action(self) -> None:
        task = self.task("Archived decision", "Body\n", next_action=None)
        self.assertNotIn("next_action", self.read(task["ref"])["properties"])
        done = self.run_mt("done", task["ref"])
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", errors="replace"))
        rejected = self.run_mt("convert", task["ref"], "note")
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(self.read(task["ref"])["properties"]["status"], "done")
        self.assertEqual(self.run_mt("validate").returncode, 0)


if __name__ == "__main__":
    unittest.main()
