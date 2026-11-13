from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE_ROOT = Path(__file__).resolve().parent.parent


class CLIUnificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root, ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"))
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text().replace("demo_workspace: true", "demo_workspace: false"))

    def run_mt(self, *args, input_bytes=None):
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root, input=input_bytes, capture_output=True)

    def json(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout)

    def snapshot(self, ref):
        return self.json(self.run_mt("read", ref, "--json"))

    def test_task_and_note_have_one_lossless_body_and_unambiguous_snapshot(self):
        source = "  Opening.  \n\n## Intent\nOrdinary heading.\n\n## Content\nAnother heading.\n\n```python\nunfinished"
        for kind in ("task", "note"):
            with self.subTest(kind=kind):
                receipt = self.json(self.run_mt("add", "--kind", kind, "--title", "Rich " + kind, "--file", "-", "--json", input_bytes=source.encode()))
                snapshot = self.snapshot(receipt["ref"])
                self.assertEqual(set(snapshot), {"ref", "path", "content_hash", "properties", "body"})
                self.assertEqual(snapshot["body"], source)
                self.assertEqual(snapshot["properties"]["kind"], kind)
                self.assertEqual(self.run_mt("read", receipt["ref"], "--body").stdout, source.encode())
                path = self.root / receipt["path"]
                original = path.read_bytes()
                unchanged = self.json(self.run_mt("edit", receipt["ref"], "--body", "--file", "-", "--expected-hash", snapshot["content_hash"], "--json", input_bytes=source.encode()))
                self.assertFalse(unchanged["changed"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_add_note_uses_shared_properties_and_discovery_filters(self):
        receipt = self.json(self.run_mt("add", "--kind", "note", "--title", "Structured note", "--file", "-", "--priority", "high", "--project", "research", "--topic", "papers", "--due", "2026-12-01", "--due-kind", "reminder", "--tag", "reading", "--hidden", "--archived", "--needs-review", "--json", input_bytes=b"## Findings\nKeep intact.\n"))
        snapshot = self.snapshot(receipt["ref"])
        properties = snapshot["properties"]
        self.assertEqual(properties["project"], "research")
        self.assertEqual(properties["tags"], ["reading"])
        self.assertTrue(properties["hidden"] and properties["archived"] and properties["needs_review"])
        self.assertEqual(snapshot["content_hash"], hashlib.sha256((self.root / receipt["path"]).read_bytes()).hexdigest())
        records = self.json(self.run_mt("list", "--kind", "note", "--needs-review", "--all", "--json"))
        self.assertEqual([item["ref"] for item in records], [receipt["ref"]])

    def test_unchanged_web_metadata_does_not_trigger_auto_sync(self):
        created = self.json(self.run_mt("note", "Body", "--title", "No change", "--json"))
        before = self.snapshot(created["ref"])
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text().replace("auto_sync: false", "auto_sync: true"))
        result = self.json(self.run_mt("card-metadata", created["ref"], "--kind", "note",
                                      "--expected-hash", before["content_hash"],
                                      "--patch", '{"title":"No change"}', "--json"))
        self.assertFalse(result["changed"])
        self.assertEqual(self.snapshot(created["ref"]), before)

    def test_properties_and_body_can_change_in_one_guarded_transaction(self):
        created = self.json(self.run_mt("add", "Initial body", "--title", "Combined edit", "--tag", "old", "--tag", "keep", "--json"))
        before = self.snapshot(created["ref"])
        text = "Updated body\n\n## Next action\nThis stays in the document.\n"
        self.json(self.run_mt("edit", created["ref"], "--body", "--file", "-", "--title", "Renamed task", "--project", "work", "--priority", "high", "--tag", "new", "--remove-tag", "old", "--hidden", "--needs-review", "--expected-hash", before["content_hash"], "--json", input_bytes=text.encode()))
        after = self.snapshot(created["ref"])
        self.assertEqual(after["body"], text)
        self.assertEqual(after["properties"]["tags"], ["keep", "new"])
        self.assertEqual(after["properties"]["project"], "work")
        self.assertEqual(after["properties"]["title"], "Renamed task")
        self.assertNotIn("next_action", after["properties"])
        self.assertNotEqual(before["content_hash"], after["content_hash"])

    def test_setting_existing_property_does_not_materialize_absent_defaults(self):
        created = self.json(self.run_mt("add", "Clear task", "--json"))
        snapshot = self.snapshot(created["ref"])
        path = self.root / created["path"]
        original = path.read_bytes()
        self.assertNotIn("project", snapshot["properties"])
        unchanged = self.json(self.run_mt("edit", created["ref"], "--set", "priority=" + snapshot["properties"]["priority"], "--json"))
        self.assertFalse(unchanged["changed"])
        self.assertEqual(path.read_bytes(), original)

    def test_creation_body_next_action_and_instructions_are_independent(self):
        scratch = Path(self.json(self.run_mt("scratch", "--json"))["path"])
        action = "Compare baselines.\nResume at Figure 2."
        instructions = "Check the source.\nDo not infer commitments."
        (scratch / "action.md").write_text(action)
        (scratch / "instructions.md").write_text(instructions)
        body = "## Next action\nThis is a paper heading.\n"
        created = self.json(self.run_mt("add", "--title", "Atomic complete task", "--file", "-", "--next-action-file", str(scratch / "action.md"), "--instructions-file", str(scratch / "instructions.md"), "--json", input_bytes=body.encode()))
        saved = self.snapshot(created["ref"])
        self.assertEqual(saved["body"], body)
        self.assertEqual(saved["properties"]["next_action"], action)
        self.assertEqual(saved["properties"]["processing_instructions"], instructions)

    def test_instruction_property_updates_and_clear_do_not_change_body(self):
        body = "## Processing instructions\n\nQuoted source guidance.\n"
        created = self.json(self.run_mt("note", "--title", "Instruction note", "--instructions", "Check source.", "--json", input_bytes=body.encode()))
        initial = self.snapshot(created["ref"])
        self.assertEqual(initial["body"], body)
        self.json(self.run_mt("edit", created["ref"], "--set", "processing_instructions=Keep original wording.\nAsk about ambiguity.", "--expected-hash", initial["content_hash"], "--json"))
        changed = self.snapshot(created["ref"])
        self.assertEqual(changed["properties"]["processing_instructions"], "Keep original wording.\nAsk about ambiguity.")
        self.assertEqual(changed["body"], body)
        self.json(self.run_mt("edit", created["ref"], "--set", "processing_instructions=", "--json"))
        self.assertNotIn("processing_instructions", self.snapshot(created["ref"])["properties"])
        conflict = self.run_mt("note", "--instructions-file", "-", input_bytes=b"ambiguous")
        self.assertNotEqual(conflict.returncode, 0)

    def test_exact_replacement_rejects_missing_repeated_overlapping_and_stale_matches(self):
        created = self.json(self.run_mt("note", "--title", "Precise edit", "--file", "-", "--json", input_bytes=b"alpha beta gamma\nrepeat repeat\n"))
        snapshot = self.snapshot(created["ref"])
        path = self.root / snapshot["path"]
        original = path.read_bytes()
        cases = ([{"old":"missing", "new":"x"}], [{"old":"repeat", "new":"x"}], [{"old":"alpha beta", "new":"x"}, {"old":"beta gamma", "new":"y"}])
        for replacements in cases:
            result = self.run_mt("edit", created["ref"], "--replace-file", "-", "--expected-hash", snapshot["content_hash"], input_bytes=json.dumps(replacements).encode())
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertEqual(path.read_bytes(), original)
        result = self.run_mt("edit", created["ref"], "--body", "--file", "-", input_bytes=b"unguarded")
        self.assertNotEqual(result.returncode, 0)
        replacements = json.dumps([{"old":"alpha beta", "new":"changed"}]).encode()
        self.json(self.run_mt("edit", created["ref"], "--replace-file", "-", "--expected-hash", snapshot["content_hash"], "--json", input_bytes=replacements))
        self.assertEqual(self.snapshot(created["ref"])["body"], "changed gamma\nrepeat repeat\n")
        after = path.read_bytes()
        stale = self.run_mt("edit", created["ref"], "--replace-file", "-", "--expected-hash", snapshot["content_hash"], input_bytes=replacements)
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(path.read_bytes(), after)

    def test_external_crlf_card_keeps_body_bytes_on_property_edit(self):
        created = self.json(self.run_mt("note", "--title", "External editor", "--file", "-", "--json", input_bytes=b"## Notes\nUnicode \xce\xbb.\n\n"))
        path = self.root / created["path"]
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        original_body = self.snapshot(created["ref"])["body"]
        self.assertIn("\r\n", original_body)
        self.json(self.run_mt("edit", created["ref"], "--set", "priority=high", "--json"))
        self.assertEqual(self.snapshot(created["ref"])["body"], original_body)
        self.assertEqual(self.run_mt("read", created["ref"], "--body").stdout, original_body.encode())

    def test_note_is_an_alias_of_add_kind_note(self):
        def card(*arguments):
            receipt = self.json(self.run_mt(*arguments, "--json", "--title", "Alias check", "--tag", "x", "--priority", "high"))
            properties = self.snapshot(receipt["ref"])["properties"]
            return {key: value for key, value in properties.items() if key not in {"id", "created", "updated"}}
        self.assertEqual(card("note", "Same body"), card("add", "--kind", "note", "Same body"))
        capture = card("note", "--capture", "Same body")
        self.assertEqual(capture, card("add", "--kind", "note", "--capture", "Same body"))
        self.assertTrue(capture["needs_review"])
        task_capture = card("add", "--capture", "Same body")
        self.assertEqual(task_capture["kind"], "task")
        self.assertNotIn("needs_review", task_capture)

        def options(command):
            text = self.run_mt(*command, "--help").stdout.decode()
            return {word.rstrip(",") for line in text.splitlines() if line.startswith("  -")
                    for word in line.split() if word.startswith("--")}
        self.assertEqual(options(["add"]) - options(["note"]), {"--kind"})
        self.assertEqual(options(["note"]) - options(["add"]), set())

    def test_task_only_creation_options_are_rejected_for_notes_with_one_message(self):
        for prefix in (("note",), ("add", "--kind", "note")):
            for option in (("--status", "active"), ("--type", "bug"), ("--next-action", "x"),
                           ("--next-action-file", "-"), ("--by", "me")):
                with self.subTest(prefix=prefix, option=option):
                    result = self.run_mt(*prefix, "Body", *option)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(b"are task-only options", result.stderr)
        self.assertEqual(self.json(self.run_mt("list", "--kind", "note", "--json")), [])

    def test_comments_conversion_and_delete_keep_identity_and_content(self):
        created = self.json(self.run_mt("add", "Original body", "--title", "Lifecycle task", "--status", "active", "--type", "bug", "--next-action", "Compare baselines.", "--needs-review", "--json"))
        ref = created["ref"]
        self.json(self.run_mt("comment", ref, "-m", "Progress saved", "--json"))
        noted = self.json(self.run_mt("convert", ref, "note", "--json"))
        note = self.snapshot(ref)
        self.assertEqual(note["properties"]["kind"], "note")
        self.assertNotIn("status", note["properties"])
        self.assertNotIn("next_action", note["properties"])
        self.assertEqual(note["body"], "Original body")
        self.assertIn("Progress saved", [record["text"] for record in note["properties"]["comments"]])
        self.assertIn("Compare baselines.", json.dumps(note["properties"]["comments"]))
        self.assertNotIn("task_conversion_state", note["properties"])
        self.assertEqual(note["path"], created["path"])  # conversion edits the file in place
        self.assertTrue((self.root / created["path"]).exists())
        self.json(self.run_mt("convert", ref, "task", "--status", "active", "--json"))
        task = self.snapshot(ref)
        self.assertEqual(task["properties"]["type"], "task")
        self.assertNotIn("next_action", task["properties"])
        self.assertEqual(task["body"], "Original body")
        deleted = self.json(self.run_mt("delete", ref, "--json"))
        self.assertTrue(deleted["deleted"])
        self.assertFalse((self.root / deleted["path"]).exists())

    def test_bulk_tags_are_hash_guarded_and_status_flags_match_other_selectors(self) -> None:
        self.run_mt("add", "Bulk tag target")
        plan = self.json(self.run_mt(
            "bulk-edit", "--kind", "task", "--status", "inbox", "--set", 'tags=["tagged"]', "--json",
        ))
        self.assertEqual(plan["selected"], 1)
        self.assertEqual(plan["items"][0]["after"]["tags"], ["tagged"])
        applied = self.json(self.run_mt("bulk-edit", "--apply", "-", "--json",
                                        input_bytes=json.dumps(plan).encode("utf-8")))
        self.assertEqual(applied["changed"], 1)
        current = self.json(self.run_mt("list", "--kind", "task", "--status", "inbox", "--json"))
        self.assertEqual(current[0]["tags"], ["tagged"])


    def test_due_before_is_exclusive_for_list_and_search(self) -> None:
        self.run_mt("add", "Earlier boundary", "--due", "2026-12-01", "--due-kind", "deadline")
        self.run_mt("add", "At boundary", "--due", "2026-12-02", "--due-kind", "deadline")
        self.run_mt("add", "Later boundary", "--due", "2026-12-03", "--due-kind", "deadline")
        listed = self.json(self.run_mt("list", "--kind", "task", "--all", "--due-before", "2026-12-02", "--json"))
        searched = self.json(self.run_mt("search", "boundary", "--kind", "task", "--all",
                                         "--due-before", "2026-12-02", "--json"))
        self.assertEqual({item["title"] for item in listed}, {"Earlier boundary"})
        self.assertEqual({item["title"] for item in searched}, {"Earlier boundary"})
        plan = self.json(self.run_mt(
            "bulk-edit", "--kind", "task", "--due-before", "2026-12-02",
            "--set", "priority=high", "--json",
        ))
        self.assertEqual({item["title"] for item in plan["items"]}, {"Earlier boundary"})
        view = self.run_mt(
            "view", "--kind", "task", "--due-before", "2026-12-02", "--name", "before-boundary",
        )
        self.assertEqual(view.returncode, 0, view.stderr.decode("utf-8", errors="replace"))
        view_tasks = list((self.root / ".mt" / "views" / "before-boundary" / "cards").glob("*.md"))
        self.assertEqual([path.stem for path in view_tasks], [listed[0]["id"]])


    def test_archive_hidden_all_selection_matches_between_collections(self) -> None:
        for title, flags in (("Visibility ordinary", ()),
                             ("Visibility archived", ("--archived",)),
                             ("Visibility hidden", ("--hidden",)),
                             ("Visibility both", ("--archived", "--hidden"))):
            result = self.run_mt("add", title, *flags)
            self.assertEqual(result.returncode, 0, result.stderr)
        listed = self.json(self.run_mt("list", "--kind", "task", "--archived", "--json"))
        searched = self.json(self.run_mt("search", "Visibility", "--kind", "task", "--archived", "--json"))
        plan = self.json(self.run_mt("bulk-edit", "--kind", "task", "--archived",
                                     "--set", "priority=high", "--json"))
        view = self.run_mt("view", "--kind", "task", "--archived", "--name", "archived-visibility")
        self.assertEqual(view.returncode, 0, view.stderr)
        view_names = {path.stem for path in (self.root / ".mt/views/archived-visibility/cards").glob("*.md")}
        selected_names = {item["title"] for item in listed}
        self.assertEqual(selected_names, {"Visibility archived", "Visibility both"})  # only archived includes hidden ones
        self.assertEqual({item["title"] for item in searched}, selected_names)
        self.assertEqual({item["title"] for item in plan["items"]}, selected_names)
        self.assertEqual(view_names, {item["id"] for item in listed})
        every = self.json(self.run_mt("list", "--kind", "task", "--all", "--json"))
        self.assertEqual({item["title"] for item in every}, {
            "Visibility ordinary", "Visibility archived", "Visibility hidden", "Visibility both",
        })



if __name__ == "__main__":
    unittest.main()
