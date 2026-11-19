"""Tests for advisory card linting and configurable lint severities."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import card_format
import card_references
import policy_parser
from test_validate import task_file


def _render_task(slug: str, body: str, **overrides: object) -> str:
    props, _ = card_format.parse_document(task_file(slug, body))
    props.update(overrides)
    return card_format.render_document(props, body)


class BacktickedReferenceTests(unittest.TestCase):
    def test_detects_inline_backticked_refs_and_ignores_code_blocks(self) -> None:
        text = (
            "See `@1a2b` and `` @3C4D `` and `@1A2B`.\n"
            "Plain @5e6f is fine.\n"
            "Command `mt read @7a8b` is fine.\n"
            "Escaped \\`@9a0b\\` is fine.\n\n"
            "```markdown\n"
            "Example `@cafe` in a fenced block.\n"
            "```\n\n"
            "    Indented `@beef` block.\n"
        )
        self.assertEqual(card_references.backticked_references(text), ["@1a2b", "@3C4D"])


class CardLinterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "cards").mkdir()
        policy_text = (ROOT / "templates" / "settings.yaml").read_text(encoding="utf-8")
        (self.root / "settings.yaml").write_text(
            policy_text.replace("demo_workspace: true", "demo_workspace: false"),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_task(self, slug: str, body: str, **overrides: object) -> Path:
        path = self.root / "cards" / f"{slug}.md"
        path.write_text(_render_task(slug, body, **overrides), encoding="utf-8")
        return path

    def run_validate(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["MT_WORKSPACE"] = str(self.root)
        return subprocess.run(
            [str(ROOT / "scripts" / "validate"), *args],
            input=input_text,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_backticked_ref_warns_by_default_across_text_fields(self) -> None:
        self.write_task(
            "demo",
            "Follow up on `@1a2b`.\n",
            next_action="Check `@3c4d`",
            processing_instructions="Link `@5e6f`",
            comments=[{"at": "2026-11-17T10:00:00", "text": "Ping `@7a8b`"}],
        )

        result = self.run_validate()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: cards/demo.md: backticked card reference @1a2b, @3c4d, @5e6f, @7a8b", result.stderr)

    def test_changes_and_mutations_surface_warnings_on_stderr(self) -> None:
        content = _render_task("demo", "Blocked by `@1a2b`.\n")
        changes = self.run_validate(
            "--changes",
            input_text=json.dumps({"changes": {"cards/demo.md": content}, "deletions": [], "previous": {"cards/demo.md": None}}),
        )
        self.assertEqual(changes.returncode, 0, changes.stderr)
        self.assertIn("WARNING: cards/demo.md: backticked card reference @1a2b", changes.stderr)

        env = os.environ.copy()
        env["MT_WORKSPACE"] = str(self.root)
        added = subprocess.run(
            [str(ROOT / "mt"), "add", "--title", "Test card", "--file", "-"],
            input="See `@dead` for details.\n",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("WARNING:", added.stderr)
        self.assertIn("backticked card reference @dead", added.stderr)

    def test_topic_case_warns_on_mutation_against_existing_cards(self) -> None:
        self.write_task("existing", "Plain.\n", topic="Work")
        new_card = _render_task("added", "Plain.\n", topic="work")

        changes = self.run_validate(
            "--changes",
            input_text=json.dumps({"changes": {"cards/added.md": new_card}, "deletions": [], "previous": {"cards/added.md": None}}),
        )
        self.assertEqual(changes.returncode, 0, changes.stderr)
        self.assertIn("WARNING: topic 'Work' and 'work' differ only by letter case", changes.stderr)

    def test_lint_policy_off_suppresses_warnings(self) -> None:
        policy_path = self.root / "settings.yaml"
        policy_text = policy_path.read_text(encoding="utf-8")
        policy_path.write_text(
            policy_text
            .replace("backticked_ref: warn", "backticked_ref: off")
            .replace("topic_case: warn", "topic_case: off"),
            encoding="utf-8",
        )
        self.write_task("one", "See `@1a2b`.\n", topic="Work")
        self.write_task("two", "Plain text.\n", topic="work")

        result = self.run_validate()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WARNING:", result.stderr)

    def test_invalid_lint_policy_is_rejected(self) -> None:
        policy_path = self.root / "settings.yaml"
        original = policy_path.read_text(encoding="utf-8")

        for bad_block in (
            "lint:\n  unknown_rule: warn\n",
            "lint:\n  backticked_ref: error\n",
            "lint:\n  backticked_ref: fatal\n",
        ):
            policy_path.write_text(original.split("\nlint:")[0] + "\n" + bad_block, encoding="utf-8")
            with self.assertRaises(ValueError):
                policy_parser.load_policy(self.root)


if __name__ == "__main__":
    unittest.main()
