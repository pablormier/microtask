from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from card_files import card_files  # noqa: E402
import card_format  # noqa: E402


EDITOR_STUB = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

path = Path(sys.argv[-1])
if os.environ.get("EDITOR_LOG"):
    Path(os.environ["EDITOR_LOG"]).write_bytes(path.read_bytes())
mode = os.environ.get("EDITOR_MODE", "unchanged")
if mode == "write":
    path.write_bytes(os.environ.get("EDITOR_BODY", "").encode("utf-8"))
elif mode == "append":
    with path.open("ab") as output:
        output.write(os.environ.get("EDITOR_BODY", "").encode("utf-8"))
elif mode == "fail":
    path.write_bytes(b"recovery copy\n")
    raise SystemExit(7)
'''


class NoteCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name)
        self.root = temporary / "repo"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(
            ".git", ".mt", "__pycache__", ".DS_Store"
        ))
        self.editor = temporary / "editor"
        self.editor.write_text(EDITOR_STUB, encoding="utf-8")
        self.editor.chmod(0o755)
        self.env = os.environ.copy()
        for name in ("VISUAL", "EDITOR", "EDITOR_MODE", "EDITOR_BODY", "EDITOR_LOG"):
            self.env.pop(name, None)
        self.env.update({"MT_USERNAME": "note-test", "MT_AUTO_SYNC": "false"})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str, input_bytes: bytes | None = None,
               env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root,
                              env=env or self.env, input=input_bytes,
                              capture_output=True, check=False)

    def run_mt_terminal(self, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        import pty

        master, slave = pty.openpty()
        try:
            return subprocess.run([str(self.root / "mt"), *args], cwd=self.root,
                                  env=env, stdin=slave, capture_output=True, check=False)
        finally:
            os.close(master)
            os.close(slave)

    def json_output(self, result: subprocess.CompletedProcess[bytes]) -> dict:
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout.decode("utf-8"))

    def create_capture(self, body: bytes, *, capture_id: str | None = None,
                       title: str = "Quick capture") -> dict:
        capture_id = capture_id or str(uuid.uuid4())
        return self.json_output(self.run_mt(
            "note", "--capture", "--file", "-", "--capture-id", capture_id,
            "--created-at", "2026-10-13T09:31:15", "--title", title, "--json",
            input_bytes=body,
        ))

    def snapshot(self, ref: str) -> dict:
        return self.json_output(self.run_mt("read", ref, "--json"))

    def test_capture_retries_are_idempotent_and_body_is_opaque(self) -> None:
        capture_id = str(uuid.uuid4())
        source = b"  pasted\r\n## Notes\r\n```\r\nend\r\n"
        args = ("note", "--capture", "--file", "-", "--capture-id", capture_id,
                "--created-at", "2026-10-13T09:31:15", "--title", "Quick capture", "--json")
        receipt = self.json_output(self.run_mt(*args, input_bytes=source))
        path = self.root / receipt["path"]
        raw_bytes = path.read_bytes()
        props, body = card_format.parse_document(raw_bytes.decode("utf-8"), expected_kind="note")
        self.assertEqual(props["id"], path.stem)
        self.assertEqual(props["capture_id"], capture_id)
        self.assertEqual(props["created"], "2026-10-13T09:31:15")
        self.assertTrue(props["needs_review"])
        self.assertEqual(body.encode("utf-8"), source)
        self.assertEqual(receipt["content_hash"], hashlib.sha256(raw_bytes).hexdigest())

        retried = self.json_output(self.run_mt(*args, input_bytes=source))
        self.assertEqual(retried["path"], receipt["path"])
        self.assertEqual(retried["content_hash"], receipt["content_hash"])
        self.assertEqual(path.read_bytes(), raw_bytes)

        conflict = self.run_mt(*args, input_bytes=b"different")
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn(b"conflict", conflict.stderr)
        self.assertEqual(path.read_bytes(), raw_bytes)
        self.assertEqual(len(list(card_files(self.root, "note"))), 1)

    def test_concurrent_capture_retries_create_one_note(self) -> None:
        capture_id = str(uuid.uuid4())
        args = ["note", "--capture", "--file", "-", "--capture-id", capture_id,
                "--created-at", "2026-10-13T09:31:15", "--title", "Same note", "--json"]
        processes = [subprocess.Popen([str(self.root / "mt"), *args], cwd=self.root,
                      env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda process: process.communicate(b"Same content", timeout=30), processes))
        receipts = []
        for process, (output, error) in zip(processes, results):
            self.assertEqual(process.returncode, 0, error.decode("utf-8", errors="replace"))
            receipts.append(json.loads(output))
        self.assertEqual(receipts[0]["path"], receipts[1]["path"])
        self.assertEqual(len(list(card_files(self.root, "note"))), 1)

        separate = self.create_capture(b"Same content", title="Same note")
        self.assertNotEqual(separate["path"], receipts[0]["path"])
        self.assertEqual(len(list(card_files(self.root, "note"))), 2)

    def test_capture_id_conflicts_across_card_kinds(self) -> None:
        capture_id = str(uuid.uuid4())
        task = self.json_output(self.run_mt("add", "--title", "Task source", "--file", "-", "--json",
                                            input_bytes=b"Task body"))
        task_path = self.root / task["path"]
        task_raw = task_path.read_bytes().decode("utf-8")
        task_path.write_text(card_format.replace_property(task_raw, "capture_id", capture_id,
                                                          expected_kind="task"), encoding="utf-8")
        conflict = self.run_mt(
            "note", "--capture", "--file", "-", "--capture-id", capture_id,
            "--created-at", "2026-10-13T09:31:15", "--title", "Collision", "--json",
            input_bytes=b"note body",
        )
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn(b"conflict", conflict.stderr)
        self.assertEqual(list(card_files(self.root, "note")), [])

    def test_empty_body_requires_title_and_can_be_saved_with_one(self) -> None:
        failed = self.run_mt("note", "--capture", "--file", "-", "--capture-id", str(uuid.uuid4()),
                             "--created-at", "2026-10-13T10:00:00", "--json", input_bytes=b"")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(list(card_files(self.root, "note")), [])

        receipt = self.json_output(self.run_mt(
            "note", "--title", "Title only", "--file", "-", "--no-needs-review", "--json", input_bytes=b""
        ))
        snapshot = self.snapshot(receipt["ref"])
        self.assertEqual(snapshot["body"], "")
        self.assertEqual(snapshot["properties"]["title"], "Title only")
        self.assertNotIn("needs_review", snapshot["properties"])
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_editor_capture_saves_exact_body_and_template_headings_are_literal(self) -> None:
        log = Path(self.temporary.name) / "editor.log"
        env = self.env.copy()
        env.update({"VISUAL": str(self.editor), "EDITOR_LOG": str(log), "EDITOR_MODE": "unchanged"})
        unchanged = self.run_mt_terminal("note", "--template", "meeting", env=env)
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr.decode("utf-8", errors="replace"))
        template = "### Discussion\n\n### Decisions\n\n### Actions\n"
        self.assertEqual(log.read_bytes(), template.encode())
        self.assertEqual(list(card_files(self.root, "note")), [])

        source = "First line\r\n\r\n## Notes\r\n````\r\nraw\r\n````\r\n"
        env.update({"EDITOR_MODE": "write", "EDITOR_BODY": source})
        saved = self.run_mt_terminal("note", env=env)
        self.assertEqual(saved.returncode, 0, saved.stderr.decode("utf-8", errors="replace"))
        note = card_files(self.root, "note")[0]
        props, body = card_format.parse_document(note.read_bytes().decode(), expected_kind="note")
        self.assertEqual(props["title"], "First line")
        self.assertEqual(body, source)

    def test_add_kind_note_with_no_input_opens_the_same_editor_as_note(self) -> None:
        env = self.env.copy()
        env.update({"VISUAL": str(self.editor), "EDITOR_MODE": "write", "EDITOR_BODY": "Via add editor\n"})
        saved = self.run_mt_terminal("add", "--kind", "note", env=env)
        self.assertEqual(saved.returncode, 0, saved.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(card_format.parse_document(card_files(self.root, "note")[0].read_text())[0]["title"],
                         "Via add editor")
        # A task has no editor mode: no input on a terminal is an error.
        task = self.run_mt_terminal("add", env=env)
        self.assertEqual(task.returncode, 2)
        self.assertIn(b"add requires text or --file", task.stderr)

    def test_hash_guarded_body_edit_preserves_properties_and_comments(self) -> None:
        receipt = self.create_capture(b"Original\r\nbody\r\n", title="Review me")
        ref = receipt["ref"]
        comment = self.run_mt("comment", ref, "-m", "Keep this history\nwith multiple lines")
        self.assertEqual(comment.returncode, 0, comment.stderr.decode("utf-8", errors="replace"))
        before = self.snapshot(ref)
        path = self.root / before["path"]
        original = path.read_bytes()

        stale = self.run_mt("edit", ref, "--body", "--file", "-", "--title", "Changed",
                            "--expected-hash", "0" * 64, "--json", input_bytes=b"should not save")
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn(b"conflict", stale.stderr)
        self.assertEqual(path.read_bytes(), original)

        body = "New\r\n## Notes\r\nordinary heading\r\n"
        edited = self.json_output(self.run_mt(
            "edit", ref, "--body", "--file", "-", "--title", "Revised title",
            "--expected-hash", before["content_hash"], "--json", input_bytes=body.encode(),
        ))
        after = self.snapshot(ref)
        self.assertEqual(after["body"], body)
        self.assertEqual(after["properties"]["title"], "Revised title")
        self.assertEqual(after["properties"]["comments"][-1]["text"], "Keep this history\nwith multiple lines")
        self.assertEqual(edited["content_hash"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_conversion_preserves_body_comments_and_does_not_restore_removed_properties(self) -> None:
        created = self.json_output(self.run_mt(
            "add", "--title", "Decision", "--next-action", "Check one source", "--file", "-", "--json",
            input_bytes=b"Body\n## Next action\nLiteral heading.\n",
        ))
        self.run_mt("comment", created["ref"], "-m", "Existing discussion")
        original = self.snapshot(created["ref"])
        result = self.run_mt("convert", created["ref"], "note")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        note = self.snapshot(created["ref"])
        self.assertEqual(note["body"], original["body"])
        self.assertEqual(note["properties"]["comments"][0]["text"], "Existing discussion")
        self.assertNotIn("status", note["properties"])
        self.assertNotIn("next_action", note["properties"])
        self.assertIn("status: inbox", " ".join(comment["text"] for comment in note["properties"]["comments"]).lower())

        converted_back = self.run_mt("convert", created["ref"], "task")
        self.assertEqual(converted_back.returncode, 0, converted_back.stderr.decode("utf-8", errors="replace"))
        task = self.snapshot(created["ref"])
        self.assertEqual(task["body"], original["body"])
        self.assertNotIn("next_action", task["properties"])
        self.assertEqual(task["properties"]["status"], "inbox")
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_json_receipt_reports_local_success_when_auto_sync_fails(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        policy_path = self.root / "mt-policy.yaml"
        policy = policy_path.read_text(encoding="utf-8")
        policy_path.write_text(policy.replace("auto_sync: false", "auto_sync: true", 1), encoding="utf-8")
        receipt = self.create_capture(b"Initial", title="Sync later")
        self.assertIn("sync requires an 'origin' remote", str(receipt["sync_error"]))
        note = self.snapshot(receipt["ref"])
        edited = self.json_output(self.run_mt(
            "edit", receipt["ref"], "--body", "--file", "-",
            "--expected-hash", note["content_hash"], "--json", input_bytes=b"Saved locally",
        ))
        self.assertTrue(edited["changed"])
        self.assertIn("sync requires an 'origin' remote", str(edited["sync_error"]))
        self.assertEqual(self.snapshot(receipt["ref"])["body"], "Saved locally")


if __name__ == "__main__":
    unittest.main()
