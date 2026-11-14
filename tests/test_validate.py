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
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402
import card_format

FRONTMATTER = """\
---
id: {stem}
title: {stem}
kind: task
status: {status}
priority: medium
type: task
project: null
topic: null
due: null
due_kind: null
created: 2026-09-04T09:00:00
updated: 2026-09-04T09:00:00
tags: []
---
"""


def task_file(stem: str, body: str, status: str = "inbox") -> str:
    return f"{FRONTMATTER.format(stem=stem, status=status)}{body}\n"


class TriageCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name)
        self.root = temporary / "repo"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", ".DS_Store"),
        )
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text().replace("demo_workspace: true", "demo_workspace: false"))
        self.env = os.environ.copy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def validate(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), "validate"],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def policy_json(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), "policy", "--json"],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def write_task(self, stem: str, body: str, status: str = "inbox") -> Path:
        path = self.root / "cards" / f"{stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(task_file(stem, body, status), encoding="utf-8")
        return path

    def test_draft_metadata_is_validated_but_body_remains_arbitrary_markdown(self) -> None:
        created = subprocess.run(
            [str(self.root / "mt"), "draft", "create", "note", "--file", "-", "--json"],
            cwd=self.root,
            input="unfinished prose\n\n--- this is body text, not card metadata ---\n",
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        draft = json.loads(created.stdout)
        path = Path(draft["path"])

        valid = self.validate()

        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("1 draft file(s)", valid.stdout)

        path.write_text(path.read_text(encoding="utf-8").replace("revision: 1", "revision: 0"),
                        encoding="utf-8")
        invalid = self.validate()

        self.assertEqual(invalid.returncode, 1)
        self.assertIn(f"drafts/{draft['id']}.md: malformed draft revision", invalid.stderr)

    def test_malformed_draft_kind_and_mode_values_report_and_validation_continues(self) -> None:
        cases = (
            ("11111111-1111-4111-8111-111111111111", "kind", []),
            ("22222222-2222-4222-8222-222222222222", "kind", {"note": "value"}),
            ("33333333-3333-4333-8333-333333333333", "mode", []),
            ("44444444-4444-4444-8444-444444444444", "mode", {"new": "value"}),
        )
        paths = []
        for draft_id, field, value in cases:
            metadata = {
                "id": draft_id, "kind": "note", "mode": "new", "revision": 1,
                "created": "2026-09-04T09:00:00", "updated": "2026-09-04T09:00:00",
                "title": "Malformed envelope", "capture_id": draft_id,
            }
            metadata[field] = value
            path = self.root / "drafts" / f"{draft_id}.md"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(("---\n" + card_format.dump_properties(metadata) + "---\nDraft text.\n").encode())
            paths.append((path, field, path.read_bytes()))
        unrelated_card = self.write_task("unrelated-invalid-status", "Still audited.", "unsupported")
        card_before = unrelated_card.read_bytes()

        result = self.validate()

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        for path, field, original in paths:
            self.assertIn(f"drafts/{path.name}: malformed draft {field}", result.stderr)
            self.assertEqual(path.read_bytes(), original)
        self.assertIn("unrelated-invalid-status.md: invalid status 'unsupported'", result.stderr)
        self.assertEqual(unrelated_card.read_bytes(), card_before)

    def test_drafts_reject_symlinks_and_unexpected_visible_entries(self) -> None:
        drafts = self.root / "drafts"
        drafts.mkdir(exist_ok=True)
        outside = self.root.parent / "outside-draft.md"
        outside.write_text("not a canonical draft\n", encoding="utf-8")
        (drafts / "linked.md").symlink_to(outside)
        (drafts / "notes.txt").write_text("unexpected\n", encoding="utf-8")

        result = self.validate()

        self.assertEqual(result.returncode, 1)
        self.assertIn("symbolic link under drafts/: linked.md", result.stderr)
        self.assertIn("unexpected file under drafts/: notes.txt", result.stderr)

    def policy(self) -> Path:
        return self.root / "mt-policy.yaml"

    def test_valid_table_and_escaped_math_pipe_validate(self) -> None:
        self.write_task(
            "valid-table",
            "## Notes\n\n"
            "| Symbol | Meaning |\n"
            "| --- | --- |\n"
            "| $\\|x\\|$ | absolute value |\n",
        )

        result = self.validate()

        self.assertEqual(result.returncode, 0, result.stderr)




    def test_imperfect_markdown_is_valid_document_content(self) -> None:
        for index, body in enumerate(("| a | b | c |\n| --- | --- |\n| 1 |", "```python\nunfinished", "## Comments\n\n## Comments\nOrdinary headings.")):
            self.write_task(f"document-{index}", body)
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pipe_line_without_delimiter_row_is_not_a_table(self) -> None:
        self.write_task(
            "pipe-paragraph",
            "## Notes\n\n|not|a|table\n",
        )

        result = self.validate()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_required_core_statuses_are_validated(self) -> None:
        self.policy().write_text(
            self.policy().read_text(encoding="utf-8").replace('  waiting: "Waiting"\n', ""),
            encoding="utf-8",
        )

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("policy statuses must include required status 'waiting'", result.stderr)

    def test_someday_is_optional(self) -> None:
        self.policy().write_text(
            self.policy().read_text(encoding="utf-8").replace(
                '  someday: "Someday" # optional; remove if unused\n', ""
            ),
            encoding="utf-8",
        )
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_custom_status_validates_without_a_status_directory(self) -> None:
        self.policy().write_text(
            self.policy().read_text(encoding="utf-8").replace(
                '  done: "Done"\n', '  done: "Done"\n  review: "Review"\n'
            ),
            encoding="utf-8",
        )
        task = self.write_task(
            "custom-status",
            "## Notes\n\nReady for review.\n",
            status="review",
        )

        result = self.validate()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(task.parent, self.root / "cards")
        self.assertEqual(
            sorted(path.name for path in (self.root / "cards").iterdir() if path.is_dir()),
            [],
        )

    def test_unknown_status_fails(self) -> None:
        self.write_task("unknown-status", "## Notes\n\nUnknown state.\n", status="review")

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown-status.md: invalid status 'review'", result.stderr)

    def test_status_labels_must_be_non_empty(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            'statuses:\n  active: ""\n', encoding="utf-8"
        )

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("policy status 'active' must have a non-empty display label", result.stderr)

    def test_quoted_policy_labels_preserve_boolean_text_and_hashes(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            'statuses:\n'
            '  active: "true" # quoted boolean-like label\n'
            "  doing: 'Research #1' # trailing comment\n",
            encoding="utf-8",
        )

        result = self.policy_json()

        self.assertEqual(result.returncode, 0, result.stderr)
        statuses = json.loads(result.stdout)["statuses"]
        self.assertEqual(statuses["active"], "true")
        self.assertEqual(statuses["doing"], "Research #1")

    def test_duplicate_status_ids_are_rejected(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            'statuses:\n  review: "Review"\n  review: "Needs work"\n',
            encoding="utf-8",
        )

        result = self.policy_json()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate policy status ID 'review'", result.stderr)

    def test_status_ids_must_be_safe_slugs_and_notes_is_reserved(self) -> None:
        self.policy().write_text(
            self.policy().read_text(encoding="utf-8").replace(
                '  done: "Done"\n',
                '  done: "Done"\n  Review Team: "Review"\n  notes: "Notes"\n',
            ),
            encoding="utf-8",
        )

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("policy status ID must be a lowercase slug", result.stderr)
        self.assertIn("policy status ID 'notes' is reserved", result.stderr)

    def test_local_policy_cannot_add_a_status_id(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            'statuses:\n  review: "Review"\n', encoding="utf-8"
        )

        result = self.policy_json()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "local policy cannot add status ID 'review'; define it in mt-policy.yaml",
            result.stderr,
        )

    def test_local_policy_can_change_a_shared_custom_status_label(self) -> None:
        self.policy().write_text(
            self.policy().read_text(encoding="utf-8").replace(
                '  done: "Done"\n', '  done: "Done"\n  review: "Review"\n'
            ),
            encoding="utf-8",
        )
        (self.root / "mt-policy.local.yaml").write_text(
            'statuses:\n  review: "Needs review"\n', encoding="utf-8"
        )

        result = self.policy_json()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["statuses"]["review"], "Needs review")

    def test_default_status_must_be_configured_and_open(self) -> None:
        for value, message in (
            ("missing", "must name a configured status ID"),
            ("done", "must not be 'done'"),
        ):
            with self.subTest(default_status=value):
                (self.root / "mt-policy.local.yaml").write_text(
                    f"default_status: {value}\n", encoding="utf-8"
                )
                result = self.validate()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"policy default_status {message}", result.stderr)

    def test_default_username_is_validated_and_exported(self) -> None:
        local = self.root / "mt-policy.local.yaml"
        for value, expected in (('"personal-user"', "personal-user"), ("null", None)):
            with self.subTest(value=value):
                local.write_text(f"default_username: {value}\n", encoding="utf-8")
                result = self.validate()
                self.assertEqual(result.returncode, 0, result.stderr)
                exported = self.policy_json()
                self.assertEqual(exported.returncode, 0, exported.stderr)
                self.assertEqual(json.loads(exported.stdout)["default_username"], expected)
        for value in ('""', '"   "', "true", "", r'"bad\0name"'):
            with self.subTest(value=value):
                local.write_text(f"default_username: {value}\n", encoding="utf-8")
                result = self.validate()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("policy default_username must be a non-empty, single-line name or null", result.stderr)

    def test_active_and_doing_allow_placeholder_or_missing_next_action(self) -> None:
        self.write_task("active-no-next", "## Notes\n\nAn active task.\n", status="active")
        self.write_task("doing-no-next", "## Notes\n\nA started task.\n", status="doing")

        result = self.validate()

        self.assertEqual(result.returncode, 0, result.stderr)
        for path in card_files(self.root, "task"):
            properties, body = card_format.parse_document(path.read_text())
            properties["next_action"] = "TBD"
            path.write_text(card_format.render_document(properties, body))
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_doing_limit_counts_status_metadata_in_flat_task_files(self) -> None:
        for number in range(6):
            self.write_task(
                f"doing-{number}",
                "## Next action\n\nContinue the work.\n",
                status="doing",
            )

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("doing task limit exceeded: 6 tasks, maximum 5", result.stderr)

    def test_nested_directories_in_cards_are_rejected(self) -> None:
        nested = self.root / "cards" / "doing"
        nested.mkdir()
        (nested / "legacy.md").write_text(task_file("legacy", "## Notes\n", "doing"))

        result = self.validate()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected directory under cards/: doing/", result.stderr)


if __name__ == "__main__":
    unittest.main()
