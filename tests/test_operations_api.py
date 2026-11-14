"""In-process API tests for scripts/operations.py using data-only workspaces and FakeClock."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))

import card_format  # noqa: E402
import operations  # noqa: E402
import store  # noqa: E402
import support  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace  # noqa: E402


class OperationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "data-ws"
        (self.root / "cards").mkdir(parents=True)
        policy_text = (SOURCE_ROOT / "mt-policy.yaml").read_text(encoding="utf-8")
        (self.root / "mt-policy.yaml").write_text(
            policy_text.replace("demo_workspace: true", "demo_workspace: false"),
            encoding="utf-8",
        )
        self.ws = Workspace(self.root, app_root=SOURCE_ROOT)

    def write_card(
        self,
        card_id: str,
        title: str,
        *,
        kind: str = "task",
        status: str = "active",
    ) -> Path:
        fields: dict[str, object] = {
            "id": card_id,
            "title": title,
            "kind": kind,
            "priority": "medium",
            "created": "2026-06-15T09:00:00",
            "updated": "2026-06-15T09:00:00",
            "archived": False,
            "hidden": False,
            "needs_review": False,
            "tags": [],
            "project": None,
        }
        if kind == "task":
            fields["status"] = status
            fields["type"] = "task"
            fields["next_action"] = "Verify API behavior."
        path = self.root / "cards" / f"{card_id}.md"
        path.write_text(
            card_format.render_document(fields, "Card body.\n", expected_kind=kind),
            encoding="utf-8",
        )
        return path

    def test_move_comment_edit_metadata_and_complete_in_process(self) -> None:
        path = self.write_card("2026-06-15-090000-api-task", "API Task")
        ref = f"@{store.Card.read(path).short_ref}"

        moved = operations.op_move(self.ws, ref, "doing", sync=False)
        self.assertTrue(moved.changed)
        self.assertEqual(moved.card.frontmatter["status"], "doing")

        commented = operations.op_comment(self.ws, ref, ["First API comment"], sync=False)
        self.assertEqual(
            commented.card.frontmatter["comments"][-1]["text"],
            "First API comment",
        )

        edited = operations.op_edit(
            self.ws, ref, assignments=["priority=high"], sync=False
        )
        self.assertEqual(edited.card.frontmatter["priority"], "high")

        current_hash = str(edited.receipt["content_hash"])
        patched = operations.op_card_metadata(
            self.ws,
            ref,
            {"tags": ["api", "fast"]},
            expected_hash=current_hash,
            kind="task",
            sync=False,
        )
        self.assertEqual(patched.card.frontmatter["tags"], ["api", "fast"])

        with self.assertRaises(store.ConflictError):
            operations.op_card_metadata(
                self.ws,
                ref,
                {"tags": ["stale"]},
                expected_hash=current_hash,
                kind="task",
                sync=False,
            )

        done = operations.op_complete(self.ws, ref, comments=["Done via API"], sync=False)
        self.assertEqual(done.card.frontmatter["status"], "done")
        self.assertEqual(done.card.frontmatter["comments"][-1]["text"], "Done via API")

    def test_error_kinds_for_missing_and_invalid_inputs(self) -> None:
        self.write_card("2026-06-15-090100-valid", "Valid Task")
        with self.assertRaises(store.NotFoundError):
            operations.op_move(self.ws, "@00000000", "doing", sync=False)
        with self.assertRaises(store.InputError):
            operations.op_move(self.ws, "Valid Task", "not-a-status", sync=False)
        with self.assertRaises(RuntimeError):
            transaction._require_mutation_lock()

    def test_fake_clock_drives_time_tracking_without_sleeps(self) -> None:
        first_path = self.write_card("2026-06-15-090200-first", "First Timed Task")
        second_path = self.write_card("2026-06-15-090300-second", "Second Timed Task")
        first_ref = f"@{store.Card.read(first_path).short_ref}"
        second_ref = f"@{store.Card.read(second_path).short_ref}"

        clock = support.FakeClock(datetime(2026, 6, 15, 10, 0, 0))
        with clock.patch(store, operations, transaction):
            operations.op_time_start(self.ws, first_ref, sync=False)
            clock.advance(seconds=125)
            operations.op_time_start(self.ws, second_ref, sync=False)
            clock.advance(seconds=45)
            operations.op_time_end(self.ws, query=second_ref, action="stop", sync=False)

        first_entries = store.Card.read(first_path).frontmatter.get("time_entries", [])
        second_entries = store.Card.read(second_path).frontmatter.get("time_entries", [])
        self.assertEqual(len(first_entries), 1)
        self.assertEqual(first_entries[0]["elapsed_seconds"], 125)
        self.assertEqual(len(second_entries), 1)
        self.assertEqual(second_entries[0]["elapsed_seconds"], 45)


if __name__ == "__main__":
    unittest.main()
