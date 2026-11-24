"""Mutation validation stays local unless a cross-card invariant changes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
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
import card_format
import card_linter
import card_render
import cli as tasklib
import focus
import operations
import store
import transaction
import validation


class ScopedValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(SOURCE / "scripts", self.root / "scripts")
        shutil.copy2(SOURCE / "mt", self.root / "mt")
        self.root.joinpath("settings.yaml").write_text(
            SOURCE.joinpath("templates", "settings.yaml").read_text().replace("demo_workspace: true", "demo_workspace: false"))
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

    def test_mismatched_id_and_filename_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must match filename stem"):
            self.validate_changes({self.path: self.before.replace(b"id: target", b"id: other")})
        self.root.joinpath("cards/mismatched.md").write_text(task_file("wrong-id", ""))
        full = subprocess.run([str(self.root / "mt"), "validate"], capture_output=True, text=True)
        self.assertNotEqual(full.returncode, 0)
        self.assertIn("must match filename stem", full.stderr)

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

    def test_batch_and_delete_overlap_validate_replacement(self):
        capture = "12345678-1234-4234-8234-123456789abc"
        first = self.before.replace(b"id: target", f"id: target\ncapture_id: {capture}".encode())
        second = task_file("new", "").replace("id: new", f"id: new\ncapture_id: {capture}").encode()
        with self.assertRaisesRegex(ValueError, "duplicate capture_id"):
            self.validate_changes({self.path: first, "cards/new.md": second})
        with self.assertRaisesRegex(ValueError, "missing YAML frontmatter"):
            self.validate_changes({self.path: b"bad"}, deletions={self.path})

    def test_concurrent_edit_during_validation_is_preserved(self):
        concurrent = self.before + b"\nConcurrent content\n"
        with patch.object(transaction, "validate_item_changes", side_effect=lambda *a, **k: self.root.joinpath(self.path).write_bytes(concurrent)):
            with self.assertRaisesRegex(ValueError, "item changed before transaction"):
                transaction.apply_item_transaction({self.path: self.before + b"\nProposed\n"}, source_root=self.root, ws=tasklib.Workspace.at(self.root))
        self.assertEqual(self.root.joinpath(self.path).read_bytes(), concurrent)

    def test_validator_rejects_traversal_and_symlinks(self):
        self.root.joinpath("cards/link.md").symlink_to(self.root / self.path)
        for path in ("../outside.md", "cards/link.md"):
            result = subprocess.run([str(self.root / "scripts/validate"), "--changes"],
                                    input=json.dumps({"changes": {path: self.before.decode()}}), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_frozen_clock_burst_allocates_without_directory_scans_or_clock_drift(self):
        ws = tasklib.Workspace.at(self.root)
        frozen = datetime(2026, 6, 15, 10, 0, 0)
        allocated: list[str] = []
        with patch.object(Path, "glob", side_effect=AssertionError("directory glob called")), \
                patch.object(os, "scandir", side_effect=AssertionError("os.scandir called")):
            for index in range(15):
                title = "Same title" if index < 5 else f"Card {index}"
                card_id = card_render.allocate_card_id(title, ws=ws, now=frozen)
                self.assertTrue(card_id.startswith("2026-06-15-100000-"), card_id)
                self.assertNotIn(card_id, allocated)
                allocated.append(card_id)
                (self.root / "cards" / f"{card_id}.md").write_text(task_file(card_id, ""))
        self.assertEqual(len(set(allocated)), 15)

    def test_add_card_does_not_parse_unrelated_cards(self):
        ws = tasklib.Workspace.at(self.root)
        for index in range(25):
            name = f"2026-06-01-0000{index:02d}-seed-{index}"
            (self.root / "cards" / f"{name}.md").write_text(task_file(name, f"Body {index}"))
        parsed_ids: list[str] = []
        real_parse_yaml = card_format.parse_yaml

        def counting_parse(text: str) -> dict[str, object]:
            parsed = real_parse_yaml(text)
            if isinstance(parsed.get("id"), str):
                parsed_ids.append(parsed["id"])
            return parsed

        with patch.object(card_format, "parse_yaml", side_effect=counting_parse):
            result = operations.op_create(ws, "task", "Second body", title="Second card", sync=False)
        created_id = str(result.card.frontmatter["id"])
        # Zero unrelated cards are parsed; every YAML parse is for the newly created card itself.
        self.assertTrue(parsed_ids)
        self.assertEqual(set(parsed_ids), {created_id})

    def test_comment_on_card_with_topic_skips_cross_card_topic_scan(self):
        ws = tasklib.Workspace.at(self.root)
        def with_topic(raw_text: str, topic: str) -> str:
            props, body = card_format.parse_document(raw_text)
            props["topic"] = topic
            return card_format.render_document(props, body)

        self.root.joinpath("cards/other.md").write_text(with_topic(task_file("other", ""), "Work/API"))
        target_with_topic = with_topic(self.before.decode("utf-8"), "Work/API")
        self.root.joinpath(self.path).write_text(target_with_topic)
        ref = "@" + hashlib.sha256(b"target").hexdigest()[:8]
        parsed_ids: list[str] = []
        real_parse_yaml = card_format.parse_yaml

        def counting_parse(text: str) -> dict[str, object]:
            parsed = real_parse_yaml(text)
            if isinstance(parsed.get("id"), str):
                parsed_ids.append(parsed["id"])
            return parsed

        with patch.object(card_linter, "lint_workspace", side_effect=AssertionError("unexpected topic scan")), \
                patch.object(card_format, "parse_yaml", side_effect=counting_parse):
            operations.op_comment(ws, ref, ["Routine update"], sync=False)
        self.assertEqual(set(parsed_ids), {"target"})
        # Changing topic casing *does* trigger the cross-card check and warns.
        report = validation.check_changes(
            self.root,
            store.policy(ws),
            {self.path: with_topic(target_with_topic, "work/api")},
        )
        self.assertEqual(report.errors, [])
        self.assertTrue(any("Work/API" in warning and "work/api" in warning for warning in report.warnings))

    def test_sort_keys_do_not_read_clock_when_now_is_supplied(self):
        ws = tasklib.Workspace.at(self.root)
        cards = [store.Card.read(self.root / self.path)]
        rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
        fixed = datetime(2026, 6, 15, 12, 0, 0)

        class ForbiddenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                raise AssertionError("datetime.now() called during sort")

        with patch.object(focus, "datetime", ForbiddenDatetime), \
                patch.object(store, "datetime", ForbiddenDatetime):
            focus.task_display_sort_key(cards[0], rank, now=fixed)
            focus.followup_sort_key(cards[0], rank, now=fixed)
            focus.ranked_focus_candidates(cards, rank, now=fixed)

    def _seed_cards(self, count: int, *, status: str = "inbox") -> list[str]:
        names: list[str] = []
        for index in range(count):
            name = f"2026-06-01-0000{index:02d}-seed-{status}-{index}"
            (self.root / "cards" / f"{name}.md").write_text(task_file(name, f"Body {index}", status=status))
            names.append(name)
        return names

    @contextmanager
    def _record_parsed_ids(self):
        parsed_ids: list[str] = []
        real_parse_yaml = card_format.parse_yaml

        def counting_parse(text: str) -> dict[str, object]:
            parsed = real_parse_yaml(text)
            if isinstance(parsed.get("id"), str):
                parsed_ids.append(parsed["id"])
            return parsed

        with patch.object(card_format, "parse_yaml", side_effect=counting_parse), \
                patch.object(store, "parse_yaml", side_effect=counting_parse):
            yield parsed_ids

    def test_move_to_doing_skips_parsing_unrelated_non_doing_cards(self):
        ws = tasklib.Workspace.at(self.root)
        unrelated = set(self._seed_cards(20, status="inbox"))
        existing_doing = self._seed_cards(1, status="doing")[0]
        ref = "@" + hashlib.sha256(b"target").hexdigest()[:8]

        with self._record_parsed_ids() as parsed_ids:
            operations.op_move(ws, ref, "doing", sync=False)

        self.assertTrue(unrelated.isdisjoint(parsed_ids))
        self.assertNotIn(existing_doing, parsed_ids)
        self.assertEqual(set(parsed_ids), {"target"})

    def test_timer_start_and_stop_skip_parsing_unrelated_cards(self):
        ws = tasklib.Workspace.at(self.root)
        unrelated = set(self._seed_cards(20, status="inbox"))
        ref = "@" + hashlib.sha256(b"target").hexdigest()[:8]

        with self._record_parsed_ids() as start_parsed:
            operations.op_time_start(ws, ref, sync=False)
        self.assertTrue(unrelated.isdisjoint(start_parsed))
        self.assertEqual(set(start_parsed), {"target"})

        with self._record_parsed_ids() as stop_parsed:
            operations.op_time_end(ws, query=ref, sync=False)
        self.assertTrue(unrelated.isdisjoint(stop_parsed))
        self.assertEqual(set(stop_parsed), {"target"})

    def test_card_by_capture_id_skips_parsing_unrelated_cards(self):
        ws = tasklib.Workspace.at(self.root)
        unrelated = set(self._seed_cards(20, status="inbox"))
        capture_id = "12345678-1234-4234-8234-123456789abc"
        match_id = "2026-06-02-000000-captured"
        (self.root / "cards" / f"{match_id}.md").write_text(
            task_file(match_id, "Captured").replace("id: " + match_id, f"id: {match_id}\ncapture_id: {capture_id}")
        )

        with self._record_parsed_ids() as parsed_ids:
            found = store.card_by_capture_id(capture_id, ws=ws)
        self.assertIsNotNone(found)
        self.assertEqual(found.path.stem, match_id)
        self.assertTrue(unrelated.isdisjoint(parsed_ids))
        self.assertEqual(parsed_ids, [match_id])

        with self._record_parsed_ids() as missed_ids:
            missing = store.card_by_capture_id("99999999-9999-4999-8999-999999999999", ws=ws)
        self.assertIsNone(missing)
        self.assertEqual(missed_ids, [])

    def test_topic_change_skips_parsing_unrelated_topics_but_checks_matching_roots(self):
        ws = tasklib.Workspace.at(self.root)
        unrelated_topic_id = "2026-06-03-000000-personal"
        matching_topic_id = "2026-06-03-000001-work"
        (self.root / "cards" / f"{unrelated_topic_id}.md").write_text(
            task_file(unrelated_topic_id, "").replace("topic: null", "topic: Personal/Health")
        )
        (self.root / "cards" / f"{matching_topic_id}.md").write_text(
            task_file(matching_topic_id, "").replace("topic: null", "topic: Work/API")
        )
        updated_target = self.before.decode("utf-8").replace("topic: null", "topic: work/api")

        with self._record_parsed_ids() as parsed_ids:
            report = validation.check_changes(self.root, store.policy(ws), {self.path: updated_target})

        self.assertEqual(report.errors, [])
        self.assertTrue(any("Work/API" in warning and "work/api" in warning for warning in report.warnings))
        self.assertEqual(set(parsed_ids), {"target", matching_topic_id})
        self.assertNotIn(unrelated_topic_id, parsed_ids)


if __name__ == "__main__":
    unittest.main()
