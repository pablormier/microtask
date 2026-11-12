from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
import tasklib  # noqa: E402

COMMON = "priority: medium\nproject: null\ntopic: null\ndue: null\ndue_kind: null\ncreated: 2026-01-01\nupdated: 2026-01-01\ntags: []\n"
TASK = f"---\nid: t\ntitle: Task\nkind: task\nstatus: inbox\ntype: task\n{COMMON}---\nBody\n"
NOTE = f"---\nid: n\ntitle: Note\nkind: note\n{COMMON}---\nBody\n"


class KindHintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_bytes(text.encode())
        return path

    def test_hint_matches_plain_quoted_and_crlf_headers(self) -> None:
        self.assertEqual(tasklib.card_kind_hint(self.write("a.md", TASK)), "task")
        self.assertEqual(tasklib.card_kind_hint(self.write("b.md", NOTE.replace("kind: note", 'kind: "note"'))), "note")
        self.assertEqual(tasklib.card_kind_hint(self.write("c.md", NOTE.replace("\n", "\r\n"))), "note")

    def test_hint_is_none_when_header_is_unclear(self) -> None:
        for text in ("no frontmatter\n", "---\ntitle: x\n---\nkind: note\n", "---\nkind: [note]\n---\n",
                     "---\nkind: other\n---\n", "---\nkind: note # c\n---\n"):
            self.assertIsNone(tasklib.card_kind_hint(self.write("x.md", text)), text)
        self.assertIsNone(tasklib.card_kind_hint(self.root / "missing.md"))

    def test_task_only_reads_skip_notes_without_parsing_them(self) -> None:
        self.write("a-task.md", TASK)
        note = self.write("b-note.md", NOTE)
        read = tasklib.Card.read
        with patch.object(tasklib.Card, "read", side_effect=read, autospec=True) as spy:
            tasks = tasklib.read_cards(self.root, "task")
        self.assertEqual([card.path.name for card in tasks], ["a-task.md"])
        self.assertNotIn(note, [call.args[0] for call in spy.call_args_list])
        self.assertEqual([card.path.name for card in tasklib.read_cards(self.root)], ["a-task.md", "b-note.md"])
        self.assertEqual([card.path.name for card in tasklib.read_cards(self.root, "note")], ["b-note.md"])

    def test_unmatched_header_falls_back_to_full_parse(self) -> None:
        # A trailing comment defeats the cheap pattern; the full parser still finds the kind.
        self.write("n.md", NOTE.replace("kind: note", "kind: note # reference"))
        self.assertEqual([card.path.name for card in tasklib.read_cards(self.root, "note")], ["n.md"])
        self.assertEqual(tasklib.read_cards(self.root, "task"), [])

    def test_malformed_note_is_not_hidden_by_a_task_only_read_when_hint_is_unclear(self) -> None:
        self.write("bad.md", "---\ntitle: x\n---\nbody\n")
        with self.assertRaises(ValueError):
            tasklib.read_cards(self.root, "task")

    def test_read_cards_rejects_a_task_with_an_unknown_status(self) -> None:
        self.write("t.md", TASK.replace("status: inbox", "status: bogus"))
        with self.assertRaisesRegex(ValueError, "invalid or missing task status"):
            tasklib.read_cards(self.root)

    def test_validate_still_catches_a_malformed_note(self) -> None:
        (self.root / "cards").mkdir()
        (self.root / "cards" / "good.md").write_text(TASK)
        (self.root / "cards" / "bad.md").write_text(NOTE.replace("priority: medium", "priority: nope"))
        result = subprocess.run(
            [sys.executable, str(SOURCE_ROOT / "scripts" / "validate"), "--data-root", str(self.root)],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad.md", result.stderr)
        self.assertIn("priority", result.stderr)


if __name__ == "__main__":
    unittest.main()
