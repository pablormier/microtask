"""Mutation validation stays local unless a cross-card invariant changes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_validate import task_file

SOURCE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE / "scripts"))
import cli as tasklib
import store
import transaction


class ScopedValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(SOURCE / "scripts", self.root / "scripts")
        shutil.copy2(SOURCE / "mt", self.root / "mt")
        self.root.joinpath("mt-policy.yaml").write_text(
            SOURCE.joinpath("mt-policy.yaml").read_text().replace("demo_workspace: true", "demo_workspace: false"))
        self.root.joinpath("cards").mkdir()
        self.path = "cards/target.md"
        self.before = task_file("target", "## Notes\n\nOriginal.").encode()
        self.root.joinpath(self.path).write_bytes(self.before)

    def validate_changes(self, changes, **kwargs):
        tasklib.validate_item_changes(changes, source_root=self.root, ws=tasklib.Workspace.at(self.root), **kwargs)

    def test_ordinary_edit_ignores_unrelated_invalid_card_but_full_audit_fails(self):
        broken = self.root / "cards/broken.md"
        broken.write_text("invalid unrelated card")
        ref = "@" + hashlib.sha256(b"target").hexdigest()[:8]
        result = subprocess.run([str(self.root / "mt"), "comment", ref, "-m", "Scoped comment"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scoped comment", self.root.joinpath(self.path).read_text())
        self.assertEqual(broken.read_text(), "invalid unrelated card")
        full = subprocess.run([str(self.root / "mt"), "validate"], capture_output=True, text=True)
        self.assertNotEqual(full.returncode, 0)
        self.assertIn("broken.md", full.stderr)

    def test_invalid_candidate_is_rejected_without_copying_or_writing(self):
        with patch.object(shutil, "copytree", side_effect=AssertionError("copied")):
            with self.assertRaisesRegex(ValueError, "missing YAML frontmatter"):
                transaction.apply_item_transaction(
                    {self.path: b"invalid"},
                    source_root=self.root,
                    ws=tasklib.Workspace.at(self.root),
                )
        self.assertEqual(self.root.joinpath(self.path).read_bytes(), self.before)

    def test_new_duplicate_id_is_rejected(self):
        self.root.joinpath("cards/other.md").write_text(task_file("duplicate", ""))
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            self.validate_changes({self.path: self.before.replace(b"id: target", b"id: duplicate")})

    def test_new_duplicate_explicit_capture_id_is_rejected(self):
        capture = "12345678-1234-4234-8234-123456789abc"
        self.root.joinpath("cards/existing.md").write_text(task_file("existing", "").replace("id: existing", f"id: existing\ncapture_id: {capture}"))
        with self.assertRaisesRegex(ValueError, "duplicate capture_id"):
            self.validate_changes({self.path: self.before.replace(b"id: target", f"id: target\ncapture_id: {capture}".encode())})

    def test_new_duplicate_time_session_is_rejected(self):
        entry = {"session_id": "a" * 32, "start": "2026-01-01T10:00:00", "end": "2026-01-01T10:05:00", "elapsed_seconds": 300}
        def with_entry(raw):
            fields, body = tasklib.card_format.parse_document(raw)
            fields["time_entries"] = [entry]
            return tasklib.card_format.render_document(fields, body)
        self.root.joinpath("cards/other.md").write_text(with_entry(task_file("other", "")))
        with self.assertRaisesRegex(ValueError, "duplicate time session id"):
            self.validate_changes({self.path: with_entry(self.before.decode()).encode()})

    def test_doing_limit_checks_transitions_but_allows_repair(self):
        for index in range(5):
            self.root.joinpath(f"cards/other{index}.md").write_text(task_file(f"other{index}", "", "doing"))
        doing = self.before.replace(b"status: inbox", b"status: doing")
        with self.assertRaisesRegex(ValueError, "doing task limit exceeded"):
            self.validate_changes({self.path: doing})
        self.root.joinpath(self.path).write_bytes(doing)
        self.validate_changes({self.path: self.before})

    def test_batch_and_delete_overlap_validate_replacement(self):
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            self.validate_changes({self.path: self.before, "cards/new.md": self.before})
        with self.assertRaisesRegex(ValueError, "missing YAML frontmatter"):
            self.validate_changes({self.path: b"bad"}, deletions={self.path})

    def test_concurrent_edit_during_validation_is_preserved(self):
        concurrent = self.before + b"\nConcurrent content\n"
        with patch.object(transaction, "validate_item_changes", side_effect=lambda *a, **k: self.root.joinpath(self.path).write_bytes(concurrent)):
            with self.assertRaisesRegex(ValueError, "item changed before transaction"):
                transaction.apply_item_transaction({self.path: self.before + b"\nProposed\n"}, source_root=self.root)
        self.assertEqual(self.root.joinpath(self.path).read_bytes(), concurrent)

    def test_validator_rejects_traversal_and_symlinks(self):
        self.root.joinpath("cards/link.md").symlink_to(self.root / self.path)
        for path in ("../outside.md", "cards/link.md"):
            result = subprocess.run([str(self.root / "scripts/validate"), "--changes"],
                                    input=json.dumps({"changes": {path: self.before.decode()}}), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
