from __future__ import annotations

import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from card_files import card_files  # noqa: E402
import card_format

DOWN = b"\x1b[B"


class CaptureCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run([str(ROOT / "mt"), "scratch"], capture_output=True, text=True, check=True)
        cls.scratch = Path(result.stdout.strip())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch)

    def setUp(self):
        self.root = self.scratch / self.id().split(".")[-1]
        self.root.mkdir()
        shutil.copytree(ROOT / "scripts", self.root / "scripts")
        shutil.copy2(ROOT / "mt", self.root / "mt")
        policy = (ROOT / "mt-policy.yaml").read_text()
        policy = re.sub(r"auto_sync:.*", "auto_sync: false", policy)
        policy = re.sub(r"demo_workspace:.*", "demo_workspace: false", policy)
        (self.root / "mt-policy.yaml").write_text(policy)
        (self.root / "cards").mkdir()
        editor = self.root / "editor.py"
        editor.write_text("import sys\nfrom pathlib import Path\np = Path(sys.argv[-1])\np.write_text(p.read_text() + '\\nSecond line\\n## Notes\\nLiteral body')\n")
        self.start_capture()

    def start_capture(self, text="Call the dentist"):
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(
            [str(self.root / "mt"), "capture", *([text] if text else [])],
            cwd=self.root, stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm", "MT_USERNAME": "capture-test-run",
                 "VISUAL": f"{sys.executable} {self.root / 'editor.py'}"},
        )
        os.close(slave)
        self.output = b""
        self.expect(b"\xe2\x80\xba Task")

    def tearDown(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        os.close(self.master)

    def expect(self, text):
        deadline = time.monotonic() + 10
        while text not in self.output:
            if time.monotonic() > deadline:
                self.fail(f"Missing {text!r}: {self.output!r}")
            if select.select([self.master], [], [], 0.1)[0]:
                try:
                    self.output += os.read(self.master, 65536)
                except OSError:
                    self.fail(f"Process exited before {text!r}: {self.output!r}")
        self.output = self.output.split(text, 1)[1]

    def send(self, keys, expected):
        os.write(self.master, keys)
        self.expect(expected)

    def action(self, index, expected):
        self.send(DOWN * index + b"\n", expected)

    def validate(self):
        result = subprocess.run([str(self.root / "mt"), "validate"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def task_fields(self):
        card = card_files(self.root, "task")[0].read_text()
        frontmatter, _ = card_format.parse_frontmatter(card)
        return card_format.parse_yaml(frontmatter)

    def test_default_task_and_terminal_restoration(self):
        self.send(b"\n", b"\xe2\x80\xba Create")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        cards = list(card_files(self.root, "task"))
        self.assertEqual(len(cards), 1)
        self.assertIn("status: inbox", cards[0].read_text())
        self.assertIn("Call the dentist", cards[0].read_text())
        self.validate()

    def test_note_with_arrow_selection(self):
        self.send(DOWN + b"\n", b"\xe2\x80\xba Create")
        self.send(b"\n", b".md")
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(len(list(card_files(self.root, "note"))), 1)
        self.assertEqual(list(card_files(self.root, "task")), [])
        self.validate()

    def test_doing_claim_and_priority_saved_together(self):
        self.send(b"\n", b"\xe2\x80\xba Create")
        self.action(3, b"\xe2\x80\xba inbox:")
        self.send(DOWN * 2 + b"\n", b"\xe2\x80\xba Create")
        self.action(4, b"\xe2\x80\xba none:")
        self.send(DOWN + b"\n", b"\xe2\x80\xba Create")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        card = card_files(self.root, "task")[0].read_text()
        self.assertIn("status: doing", card)
        self.assertIn("capture-test-run", card)
        self.assertEqual(self.task_fields()["priority"], "urgent")
        self.validate()

    def test_escape_cancels_without_a_card(self):
        self.send(b"\x1b", b"Capture cancelled.")
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(list(card_files(self.root, "task")), [])
        self.assertEqual(list(card_files(self.root, "note")), [])

    def test_date_rejects_invalid_input_and_requires_meaning(self):
        self.send(b"\n", b"\xe2\x80\xba Create")
        self.action(5, b"Date (YYYY-MM-DD")
        self.send(b"2026-02-30\n", b"Enter a valid date")
        self.expect(b"\xe2\x80\xba Create")
        self.action(5, b"Date (YYYY-MM-DD")
        self.send(b"2026-12-01\n", b"\xe2\x80\xba Choose meaning")
        self.send(b"\n", b"Choose a date meaning")
        self.send(DOWN + b"\n", b"\xe2\x80\xba Create")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(self.task_fields()["due"], "2026-12-01")
        self.assertEqual(self.task_fields()["due_kind"], "deadline")
        self.validate()

    def test_ctrl_c_cancels_review(self):
        self.send(b"\n", b"\xe2\x80\xba Create")
        # A PTY without a controlling session does not deliver terminal signals.
        self.process.send_signal(signal.SIGINT)
        self.expect(b"Capture cancelled.")
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(list(card_files(self.root, "task")), [])

    def test_non_terminal_fails_with_guidance(self):
        result = subprocess.run([str(self.root / "mt"), "capture", "test"], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"use mt add or mt note", result.stderr)

    def test_bare_capture_uses_changed_policy_defaults(self):
        self.process.kill()
        self.process.wait()
        os.close(self.master)
        policy_path = self.root / "mt-policy.yaml"
        policy_path.write_text(policy_path.read_text().replace("default_status: inbox", "default_status: active")
                               .replace("default_priority: none", "default_priority: high"))
        self.start_capture("")
        self.send(b"\n", b"> ")
        self.send(b"A clear task\n", b"> ")
        self.send(b"\x04", b"\xe2\x80\xba Create")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(self.task_fields()["status"], "active")
        self.assertEqual(self.task_fields()["priority"], "high")
        self.validate()

    def test_multiline_prompts_preserve_blank_lines_and_unicode(self):
        self.process.kill()
        self.process.wait()
        os.close(self.master)
        self.start_capture("")
        self.send(b"\n", b"> ")
        for line in ["A café idea", "", "## Notes", "  keep indentation"]:
            self.send((line + "\n").encode(), b"> ")
        self.send(b"\x04", b"\xe2\x80\xba Create")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        card = card_files(self.root, "task")[0].read_text()
        body = card_format.parse_document(card)[1]
        self.assertEqual(body, "A café idea\n\n## Notes\n  keep indentation")
        self.validate()

    def test_ctrl_d_finishes_partial_line_without_enter(self):
        self.process.kill()
        self.process.wait()
        os.close(self.master)
        self.start_capture("")
        self.send(DOWN + b"\n", b"> ")
        self.send(b"One line note\x04", b"\xe2\x80\xba Create")
        self.send(b"\n", b".md")
        self.assertEqual(self.process.wait(timeout=10), 0)
        card = card_files(self.root, "note")[0].read_text()
        self.assertEqual(card_format.parse_document(card)[1].strip(), "One line note")
        self.validate()

    def test_ctrl_c_cancels_multiline_input(self):
        self.process.kill()
        self.process.wait()
        os.close(self.master)
        self.start_capture("")
        self.send(b"\n", b"> ")
        self.send(b"Unfinished text\n", b"> ")
        self.process.send_signal(signal.SIGINT)
        self.expect(b"Capture cancelled.")
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(list(card_files(self.root, "task")), [])

    def test_editor_preserves_multiline_text(self):
        self.send(b"\n", b"\xe2\x80\xba Create")
        self.action(1, b"\xe2\x80\xba Create")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        card = card_files(self.root, "task")[0].read_text()
        body = card_format.parse_document(card)[1]
        self.assertEqual(body, "Call the dentist\nSecond line\n## Notes\nLiteral body")
        self.validate()

    def test_more_properties_and_title(self):
        self.send(b"\n", b"\xe2\x80\xba Create")
        self.action(2, b"Optional title:")
        self.send(b"Book appointment\n", b"\xe2\x80\xba Create")
        self.action(7, b"\xe2\x80\xba Type")
        self.send(DOWN + b"\n", b"Project:")
        self.send(b"Health admin\n", b"\xe2\x80\xba Create")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        self.assertEqual(self.task_fields()["project"], "Health admin")
        self.assertEqual(card_format.parse_document(card_files(self.root, "task")[0].read_text())[0]["title"], "Book appointment")
        self.validate()

    def test_processing_instructions_are_multiline_and_reviewed_before_save(self):
        self.send(b"\n", b"\xe2\x80\xba Create")
        self.action(6, b"Processing instructions")
        instructions = "Extract decisions.\n\nList owners."
        self.expect(b"Enter: new line")
        self.send(b"Extract decisions.\n\nList owners.\x04", b"  Review task")
        self.expect(b"Extract decisions.")
        self.expect(b"List owners.")
        self.send(b"\n", b"Created @")
        self.assertEqual(self.process.wait(timeout=10), 0)
        card = card_files(self.root, "task")[0].read_text()
        self.assertEqual(card_format.parse_document(card)[0]["processing_instructions"], instructions)
        self.validate()


if __name__ == "__main__":
    unittest.main()
