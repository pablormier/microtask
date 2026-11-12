from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class PropertyPatchAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"))
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text().replace("demo_workspace: true", "demo_workspace: false"))

    def run_mt(self, *args, input_bytes: bytes | None = None):
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root,
                              input=input_bytes, capture_output=True)

    def json(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout)

    def create_card(self, kind: str, *, due: str | None = None):
        args = ["add" if kind == "task" else "note", "--title", f"Audit {kind}", "--file", "-", "--json"]
        if due:
            args.extend(["--due", due, "--due-kind", "deadline"])
        return self.json(self.run_mt(*args, input_bytes=b"Body\n"))

    def test_cli_due_null_clears_due_kind_for_tasks_and_notes(self):
        for kind in ("task", "note"):
            with self.subTest(kind=kind):
                created = self.create_card(kind, due="2026-12-01")
                result = self.json(self.run_mt("edit", created["ref"], "--set", "due=null", "--json"))
                self.assertTrue(result["changed"])
                card = self.json(self.run_mt("read", created["ref"], "--json"))
                self.assertNotIn("due", card["properties"])
                self.assertNotIn("due_kind", card["properties"])
                validated = self.run_mt("validate")
                self.assertEqual(validated.returncode, 0, validated.stderr.decode("utf-8", errors="replace"))

    def test_dashboard_semantic_noop_preserves_human_yaml_and_explicit_nulls(self):
        created = self.create_card("note")
        path = self.root / created["path"]
        raw = path.read_bytes()
        marker = b"priority: none\n"
        self.assertIn(marker, raw)
        raw = raw.replace(marker, b"# Keep this formatting comment.\npriority: none\nproject: null\ndue: null\ndue_kind: null\n", 1)
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        patch = json.dumps({"title": "Audit note", "project": None, "due": None, "due_kind": None})
        result = self.json(self.run_mt("card-metadata", created["ref"], "--kind", "note",
                                       "--expected-hash", digest, "--patch", patch, "--json"))
        self.assertFalse(result["changed"])
        self.assertEqual(path.read_bytes(), raw)

    def test_malformed_due_kind_is_a_validation_error_not_a_traceback(self):
        created = self.create_card("task", due="2026-12-01")
        path = self.root / created["path"]
        raw = path.read_bytes()
        self.assertIn(b"due_kind: deadline", raw)
        path.write_bytes(raw.replace(b"due_kind: deadline", b"due_kind: []", 1))
        result = self.run_mt("validate")
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn(b"due_kind", output)
        self.assertNotIn(b"Traceback", output)

    def test_patch_does_not_use_python_boolean_numeric_equality(self):
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import card_format
        import tasklib

        properties, _ = card_format.parse_document(tasklib.render_new_card("note", "audit", "Audit"))
        properties["hidden"] = False
        with self.assertRaisesRegex(ValueError, "hidden must be true or false"):
            card_format.apply_property_patch(properties, {"hidden": 0}, kind="note")


if __name__ == "__main__":
    unittest.main()
