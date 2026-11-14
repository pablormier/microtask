from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import card_format
import support

ROOT = Path(__file__).resolve().parents[1]


class EditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(
            ".git", ".mt", ".DS_Store"
        ))
        self.env = {**os.environ, "MT_USERNAME": "edit-test", "MT_AUTO_SYNC": "false"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return support.run_cli(self.root, *args, input_text=input_text, env=self.env)

    def json(self, *args: str, input_text: str | None = None) -> dict:
        result = self.run_mt(*args, input_text=input_text)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def create_task(self, title: str, body: str = "") -> dict:
        return self.json("add", "--title", title, "--file", "-", "--json", input_text=body)

    def create_note(self, title: str, body: str = "") -> dict:
        return self.json("note", "--title", title, "--file", "-", "--json", input_text=body)

    def test_property_edit_preserves_crlf_body_and_structured_properties(self) -> None:
        created = self.create_task("Lossless card", "alpha\nbeta\r\n\r\n## Notes\r\nliteral\r\n")
        path = self.root / created["path"]
        original = path.read_bytes()
        properties, body = card_format.parse_document(original.decode())
        self.assertEqual(body, "alpha\nbeta\r\n\r\n## Notes\r\nliteral\r\n")
        updated = self.run_mt("edit", created["ref"], "--set", "priority=high", "--json")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        after = path.read_bytes()
        next_properties, next_body = card_format.parse_document(after.decode())
        self.assertEqual(next_body.encode(), body.encode())
        self.assertEqual(next_properties["priority"], "high")
        self.assertEqual(next_properties["id"], properties["id"])
        receipt = json.loads(updated.stdout)
        self.assertEqual(set(receipt), {"ref", "path", "content_hash", "updated", "changed", "properties", "body"})
        self.assertEqual(receipt["content_hash"], hashlib.sha256(after).hexdigest())

    def test_body_read_json_has_one_logical_representation(self) -> None:
        body = "  text\n\n## Content\n\n```microtask-capture\nliteral\n````\n"
        task = self.create_task("Body snapshot", body)
        snapshot = self.json("read", task["ref"], "--json")
        self.assertEqual(set(snapshot), {"ref", "path", "content_hash", "properties", "body"})
        self.assertEqual(snapshot["body"], body)
        self.assertEqual(self.run_mt("read", task["ref"], "--body").stdout, body)
        self.assertEqual(self.run_mt("read", task["ref"]).stdout.encode(), (self.root / task["path"]).read_bytes())

    def test_combined_property_and_body_replacement_is_one_guarded_transaction(self) -> None:
        created = self.create_note("Combined", "Original body\r\n")
        initial = self.json("read", created["ref"], "--json")
        replacement = "New body\r\n\n# Literal title\r\n"
        edited = self.json("edit", created["ref"], "--title", "Updated title", "--body", "--file", "-",
                           "--expected-hash", initial["content_hash"], "--json", input_text=replacement)
        saved = self.json("read", created["ref"], "--json")
        self.assertEqual(saved["properties"]["title"], "Updated title")
        self.assertEqual(saved["body"], replacement)
        self.assertTrue(edited["changed"])
        self.assertNotEqual(edited["content_hash"], initial["content_hash"])

    def test_append_preserves_existing_body_and_accepts_unfinished_markdown(self) -> None:
        body = "| a | b |\n|---|---|\n| unfinished |\n\n```python\n"
        created = self.create_task("Append", body)
        addition = "## This is ordinary content\n\n$ x^2 $"
        self.run_mt("edit", created["ref"], "--body", "--append", "--file", "-", input_text=addition)
        saved = self.json("read", created["ref"], "--json")
        self.assertEqual(saved["body"], body + "\n" + addition)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_exact_replacements_use_original_body_and_roll_back_on_any_error(self) -> None:
        created = self.create_task("Exact replace", "one alpha two alpha three\n")
        snapshot = self.json("read", created["ref"], "--json")
        path = self.root / created["path"]
        original = path.read_bytes()
        for replacement, message in (([{"old": "absent", "new": "x"}], "exactly once"),
                                     ([{"old": "alpha", "new": "x"}], "exactly once"),
                                     ([{"old": "one alpha", "new": "x"}, {"old": "alpha two", "new": "y"}], "overlap")):
            failed = self.run_mt("edit", created["ref"], "--replace-file", "-", "--expected-hash",
                                 snapshot["content_hash"], input_text=json.dumps(replacement))
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(message, failed.stderr)
            self.assertEqual(path.read_bytes(), original)
        exact = [{"old": "one", "new": "1"}, {"old": "three", "new": "3"}]
        saved = self.json("edit", created["ref"], "--replace-file", "-", "--expected-hash",
                          snapshot["content_hash"], "--json", input_text=json.dumps(exact))
        body = self.run_mt("read", created["ref"], "--body")
        self.assertEqual(body.stdout, "1 alpha two alpha 3\n")
        self.assertTrue(saved["changed"])

    def test_old_new_replacements_and_optional_hash(self) -> None:
        created = self.create_task("Direct", "one alpha two alpha three\n")
        ref, path = created["ref"], self.root / created["path"]
        original = path.read_bytes()
        digest = self.json("read", ref, "--json")["content_hash"]
        cases = (
            (("--old", "absent", "--new", "x"), "found 0 matches"),
            (("--old", "alpha", "--new", "x"), "found 2 matches"),
            (("--old", "one alpha", "--new", "x", "--old", "alpha two", "--new", "y"), "overlap"),
            (("--old", "one", "--new", "1", "--old", "two", "--new", "2", "--expected-hash", "0" * 64), "conflict"),
        )
        for arguments, message in cases:
            failed = self.run_mt("edit", ref, *arguments)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn(message, failed.stderr)
            self.assertEqual(path.read_bytes(), original)
        mismatch = self.run_mt("edit", ref, "--old", "one", "--new", "1", "--old", "two")
        self.assertEqual(mismatch.returncode, 2)
        self.assertIn("same number of times", mismatch.stderr)
        for extra in (("--replace-file", "-"), ("--body", "--file", "-")):
            exclusive = self.run_mt("edit", ref, "--old", "one", "--new", "1", *extra, input_text="[]")
            self.assertEqual(exclusive.returncode, 2)
            self.assertEqual(path.read_bytes(), original)
        saved = self.json("edit", ref, "--old", "one", "--new", "1", "--old", "three", "--new", "3",
                          "--set", "priority=high", "--json")
        self.assertTrue(saved["changed"])
        self.assertEqual(saved["body"], "1 alpha two alpha 3\n")
        guarded = self.json("edit", ref, "--old", "two", "--new", "2", "--expected-hash", saved["content_hash"], "--json")
        self.assertEqual(guarded["body"], "1 alpha 2 alpha 3\n")
        self.assertNotEqual(digest, guarded["content_hash"])

    def test_replace_file_hash_is_optional_and_errors_are_specific(self) -> None:
        created = self.create_task("Replace file", "alpha beta\n")
        ref, path = created["ref"], self.root / created["path"]
        original = path.read_bytes()
        broken = self.run_mt("edit", ref, "--replace-file", "-", input_text='[{"old": "a\\q", "new": "b"}]')
        self.assertEqual(broken.returncode, 2)
        self.assertIn("line 1, column", broken.stderr)
        self.assertIn("<<'EOF'", broken.stderr)
        wrong_shape = self.run_mt("edit", ref, "--replace-file", "-", input_text='{"old": "a", "new": "b"}')
        self.assertIn('a JSON array of {"old": ..., "new": ...} objects', wrong_shape.stderr)
        bad_entry = self.run_mt("edit", ref, "--replace-file", "-",
                                input_text='[{"old": "alpha", "new": "x"}, {"old": "beta"}]')
        self.assertIn("entry 2", bad_entry.stderr)
        stale = self.run_mt("edit", ref, "--replace-file", "-", "--expected-hash", "1" * 64,
                            input_text='[{"old": "alpha", "new": "x"}]')
        self.assertIn("conflict", stale.stderr)
        self.assertEqual(path.read_bytes(), original)
        saved = self.json("edit", ref, "--replace-file", "-", "--json", input_text='[{"old": "alpha", "new": "x"}]')
        self.assertEqual(saved["body"], "x beta\n")

    def test_mutation_json_returns_saved_card_and_text_says_validated(self) -> None:
        created = self.create_task("Receipts", "body\n")
        read = self.json("read", created["ref"], "--json")
        self.assertEqual({key: created[key] for key in read}, read)
        self.assertTrue(created["changed"])
        operations = (
            ("edit", created["ref"], "--body", "--append", "--file", "-", "--json"),
            ("comment", created["ref"], "-m", "note", "--json"),
            ("move", created["ref"], "doing", "--json"),
            ("done", created["ref"], "--json"),
        )
        for operation in operations:
            result = self.json(*operation, input_text="more")
            current = self.json("read", result["ref"], "--json")
            self.assertEqual({key: result[key] for key in current}, current, operation[0])
            self.assertIn("changed", result)
            if operation[0] == "edit":
                self.assertIn("more", result["body"])
        plain = self.create_task("Plain", "x\n")
        self.assertIn("Edited and validated:", self.run_mt("edit", plain["ref"], "--set", "priority=low").stdout)
        self.assertIn("Commented and validated:", self.run_mt("comment", plain["ref"], "-m", "hi").stdout)
        self.assertIn("Moved to doing and validated:", self.run_mt("move", plain["ref"], "doing").stdout)
        self.assertIn("Completed and validated:", self.run_mt("done", plain["ref"]).stdout)

    def test_body_replacement_requires_hash_and_stale_hash_never_writes(self) -> None:
        created = self.create_task("CAS", "Before\n")
        snapshot = self.json("read", created["ref"], "--json")
        path = self.root / created["path"]
        original = path.read_bytes()
        missing = self.run_mt("edit", created["ref"], "--body", "--file", "-", input_text="Missing guard")
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(path.read_bytes(), original)
        self.run_mt("edit", created["ref"], "--set", "priority=high")
        current = path.read_bytes()
        stale = self.run_mt("edit", created["ref"], "--body", "--file", "-", "--expected-hash",
                            snapshot["content_hash"], input_text="Stale")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("conflict", stale.stderr)
        self.assertEqual(path.read_bytes(), current)

    def test_noop_does_not_change_bytes_or_timestamp(self) -> None:
        created = self.create_task("No-op")
        path = self.root / created["path"]
        original = path.read_bytes()
        result = self.run_mt("edit", created["ref"], "--set", "priority=none", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["changed"], False)
        self.assertEqual(path.read_bytes(), original)

    def test_task_next_action_and_note_instructions_are_optional_multiline_properties(self) -> None:
        task = self.create_task("Properties")
        initial = self.json("read", task["ref"], "--json")
        next_action = "First step\n\nSecond step"
        self.json("edit", task["ref"], "--set", "next_action=" + next_action, "--json")
        note = self.create_note("Guidance")
        self.json("edit", note["ref"], "--set", "processing_instructions=Read this\n\nThen compare", "--json")
        task_props = self.json("read", task["ref"], "--json")["properties"]
        note_props = self.json("read", note["ref"], "--json")["properties"]
        self.assertEqual(task_props["next_action"], next_action)
        self.assertEqual(note_props["processing_instructions"], "Read this\n\nThen compare")
        self.assertNotIn("next_action", note_props)
        self.assertNotIn("processing_instructions", initial["properties"])
        self.assertEqual(self.run_mt("validate").returncode, 0)


if __name__ == "__main__":
    unittest.main()
