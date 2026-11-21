"""Tests for SQLite local history (.mt/history.sqlite) and mt history / mt restore."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import history  # noqa: E402
import operations  # noqa: E402
import policy_parser  # noqa: E402
import store  # noqa: E402
import transaction  # noqa: E402
from workspace import Workspace, create_workspace  # noqa: E402


class LocalHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ws"
        create_workspace(self.root, ROOT)
        settings = self.root / "settings.yaml"
        text = settings.read_text(encoding="utf-8")
        settings.write_text(text.replace("auto_sync: true", "auto_sync: false"), encoding="utf-8")
        self.ws = Workspace.at(self.root)

    def mt(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["MT_WORKSPACE"] = str(self.root)
        return subprocess.run(
            [str(ROOT / "mt"), *args],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_schema_and_roundtrip_crud_events(self) -> None:
        created = operations.op_create(self.ws, "task", "Initial body\n", title="Alpha Task", sync=False)
        card = created.card
        assert card is not None
        rel = str(card.path.relative_to(self.root))

        operations.op_edit(self.ws, f"@{card.short_ref}", body_mode="replace", body_text="Updated body\n", by="alice", sync=False)
        operations.op_comment(self.ws, f"@{card.short_ref}", ["A comment"], by="bob", sync=False)
        operations.op_delete(self.ws, f"@{card.short_ref}", by="root", sync=False)
        self.assertFalse(card.path.exists())

        db_path = self.ws.history_db_path()
        self.assertTrue(db_path.is_file())
        conn = sqlite3.connect(str(db_path))
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
        finally:
            conn.close()

        events = history.list_events(self.ws, limit=20)
        self.assertEqual([ev["action"] for ev in events], ["delete", "comment", "edit", "create"])
        deleted = history.list_events(self.ws, deleted_only=True)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["revisions"][0]["path"], rel)
        self.assertEqual(deleted[0]["revisions"][0]["card_id"], card.frontmatter["id"])
        self.assertEqual(deleted[0]["revisions"][0]["title"], "Alpha Task")

        by_ref = self.mt("history", f"@{card.short_ref}", "--json")
        self.assertEqual(by_ref.returncode, 0, by_ref.stderr)
        self.assertEqual(len(json.loads(by_ref.stdout)), 4)

        detail = history.get_event(self.ws, deleted[0]["id"])
        assert detail is not None
        rev = detail["revisions"][0]
        self.assertIsNotNone(rev["before_text"])
        self.assertIsNone(rev["after_text"])
        self.assertIn("Updated body", rev["before_text"])
        self.assertIn("-Updated body", rev["diff"])

    def test_out_of_band_shell_edit_is_captured_before_delete(self) -> None:
        created = operations.op_create(self.ws, "note", "v1\n", title="Shell Note", sync=False)
        card = created.card
        assert card is not None
        card.path.write_text(card.path.read_text(encoding="utf-8").replace("v1", "shell-v2"), encoding="utf-8")

        operations.op_delete(self.ws, f"@{card.short_ref}", by="root", sync=False)
        deleted = history.list_events(self.ws, deleted_only=True)
        detail = history.get_event(self.ws, deleted[0]["id"])
        assert detail is not None
        self.assertIn("shell-v2", detail["revisions"][0]["before_text"])

    def test_cli_history_show_diff_and_restore(self) -> None:
        created = operations.op_create(self.ws, "task", "Line 1\n", title="Recover Me", sync=False)
        card = created.card
        assert card is not None
        original_text = card.path.read_text(encoding="utf-8")
        operations.op_delete(self.ws, f"@{card.short_ref}", by="root", sync=False)

        listed = self.mt("history", "--deleted", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        events = json.loads(listed.stdout)
        self.assertEqual(len(events), 1)
        event_id = events[0]["id"]

        shown = self.mt("history", "show", str(event_id), "--diff")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("Recover Me", shown.stdout)
        self.assertIn("-Line 1", shown.stdout)

        restored = self.mt("restore", str(event_id), "--json")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(card.path.read_text(encoding="utf-8"), original_text)

        # Refuses to overwrite existing card without --force.
        conflict = self.mt("restore", str(event_id))
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("--force", conflict.stderr)

        forced = self.mt("restore", str(event_id), "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_upload_event_records_metadata_without_blob(self) -> None:
        res = operations.op_store_attachment(self.ws, b"hello attachment", filename="notes.txt", by="alice", sync=False)
        self.assertEqual(res.history_warning, "")
        events = history.list_events(self.ws, limit=5)
        self.assertEqual(events[0]["action"], "upload")
        self.assertEqual(events[0]["actor"], "alice")
        detail = history.get_event(self.ws, events[0]["id"])
        assert detail is not None
        self.assertIsNone(detail["revisions"][0]["before_bytes"])
        self.assertIsNone(detail["revisions"][0]["after_bytes"])

    def test_failure_modes_warning_vs_required(self) -> None:
        created = operations.op_create(self.ws, "task", "Body\n", title="Failure Test", sync=False)
        card = created.card
        assert card is not None
        rel = str(card.path.relative_to(self.root))

        # Non-strict write succeeds and sets ctx.warning on DB failure.
        with mock.patch.object(history, "record_transaction", side_effect=sqlite3.OperationalError("read-only")):
            res = operations.op_comment(self.ws, f"@{card.short_ref}", ["Still saved"], by="alice", sync=False)
        self.assertIn("read-only", res.history_warning)
        self.assertIn("Still saved", card.path.read_text(encoding="utf-8"))

        # Strict pre-snapshot raises HistoryRequiredError and leaves file untouched.
        before = card.path.read_bytes()
        with mock.patch.object(history, "_open_db", side_effect=sqlite3.OperationalError("read-only")):
            with history.capture_context(action="delete", actor="root", source="web", require=True):
                with self.assertRaises(store.HistoryRequiredError):
                    with transaction.time_mutation_lock(self.ws):
                        transaction.apply_item_transaction({}, deletions={rel}, ws=self.ws)
        self.assertEqual(card.path.read_bytes(), before)

    def test_rules_local_history_false_disables_recording_and_snapshot_check(self) -> None:
        settings = self.root / "settings.yaml"
        text = settings.read_text(encoding="utf-8").replace("auto_sync: false", "auto_sync: false\n  local_history: false")
        settings.write_text(text, encoding="utf-8")
        self.ws = Workspace.at(self.root)

        created = operations.op_create(self.ws, "task", "No History", sync=False)
        card = created.card
        assert card is not None
        rel = str(card.path.relative_to(self.root))
        self.assertFalse(self.ws.history_db_path().exists())

        with history.capture_context(action="delete", actor="root", source="web", require=True):
            with transaction.time_mutation_lock(self.ws):
                transaction.apply_item_transaction({}, deletions={rel}, ws=self.ws)
        self.assertFalse(card.path.exists())
        self.assertFalse(self.ws.history_db_path().exists())

    def test_policy_parser_validates_local_history_boolean(self) -> None:
        settings = self.root / "settings.yaml"
        text = settings.read_text(encoding="utf-8").replace("auto_sync: false", "auto_sync: false\n  local_history: 'yes'")
        settings.write_text(text, encoding="utf-8")
        with self.assertRaises(ValueError):
            policy_parser.load_policy(self.root)

    def test_prune_history(self) -> None:
        for idx in range(5):
            operations.op_create(self.ws, "note", f"Note {idx}", sync=False)
        self.assertEqual(len(history.list_events(self.ws, limit=20)), 5)
        deleted = history.prune_history(self.ws, max_events=2)
        self.assertEqual(deleted, 3)
        self.assertEqual(len(history.list_events(self.ws, limit=20)), 2)

    def test_commit_failure_aborts_before_delete_and_fs_failure_cleans_up_event(self) -> None:
        created = operations.op_create(self.ws, "task", "Durable\n", title="Commit First", sync=False)
        card = created.card
        assert card is not None
        before = card.path.read_bytes()

        # Simulate COMMIT failing during prepare_transaction: card must remain untouched.
        real_open = history._open_db

        class FailCommitConn:
            def __init__(self, inner: sqlite3.Connection) -> None:
                self._inner = inner

            @property
            def in_transaction(self) -> bool:
                return self._inner.in_transaction

            def execute(self, sql: str, *args, **kwargs):
                if sql.strip().upper() == "COMMIT":
                    raise sqlite3.OperationalError("disk full on commit")
                return self._inner.execute(sql, *args, **kwargs)

            def executemany(self, *args, **kwargs):
                return self._inner.executemany(*args, **kwargs)

            def close(self) -> None:
                self._inner.close()

        with mock.patch.object(history, "_open_db", side_effect=lambda ws: FailCommitConn(real_open(ws))):
            with self.assertRaises(store.HistoryRequiredError):
                operations.op_delete(self.ws, f"@{card.short_ref}", by="root", sync=False)
        self.assertEqual(card.path.read_bytes(), before)

        # Simulate filesystem unlink failing after snapshot pre-commit: event is cleaned up.
        events_before = len(history.list_events(self.ws, limit=20))
        with mock.patch.object(Path, "unlink", side_effect=OSError("unlink failed")):
            with self.assertRaises(store.ValidationError):
                operations.op_delete(self.ws, f"@{card.short_ref}", by="root", sync=False)
        self.assertEqual(card.path.read_bytes(), before)
        self.assertEqual(len(history.list_events(self.ws, limit=20)), events_before)

    def test_operations_api_enforces_destructive_history_policy(self) -> None:
        created = operations.op_create(self.ws, "task", "Direct API\n", title="Direct Delete", sync=False)
        card = created.card
        assert card is not None
        with mock.patch.object(history, "_open_db", side_effect=sqlite3.OperationalError("read-only")):
            with self.assertRaises(store.HistoryRequiredError):
                operations.op_delete(self.ws, f"@{card.short_ref}", by="root", sync=False)
        self.assertTrue(card.path.exists())

    def test_forced_restore_preserves_claim_and_timer_safeguards(self) -> None:
        created = operations.op_create(self.ws, "task", "v1\n", title="Guarded Task", sync=False)
        card = created.card
        assert card is not None
        operations.op_edit(self.ws, f"@{card.short_ref}", body_mode="replace", body_text="v2\n", by="alice", sync=False)
        edit_event = history.list_events(self.ws, limit=5)[0]

        # Move to doing as alice (claims the task).
        operations.op_move(self.ws, f"@{card.short_ref}", "doing", by="alice", sync=False)
        with self.assertRaises(store.ConflictError):
            operations.op_restore(self.ws, edit_event["id"], force=True, by="bob", sync=False)

        # Start a timer as alice: even alice cannot overwrite a task with a running timer.
        operations.op_time_start(self.ws, f"@{card.short_ref}", by="alice", sync=False)
        with self.assertRaises(store.ConflictError):
            operations.op_restore(self.ws, edit_event["id"], force=True, by="alice", sync=False)

    def test_corrupt_db_and_unknown_schema_version_are_handled_cleanly(self) -> None:
        # Unknown future schema version is rejected.
        db_path = self.ws.history_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA user_version = 99")
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(sqlite3.DatabaseError):
            history.list_events(self.ws)
        res = self.mt("history")
        self.assertEqual(res.returncode, 1)
        self.assertIn("could not read local history", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

        # Corrupt bytes on disk produce a clean CLI error, not a traceback.
        db_path.write_bytes(b"not a valid sqlite database")
        res = self.mt("history")
        self.assertEqual(res.returncode, 1)
        self.assertIn("could not read local history", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_failed_rollback_keeps_prepared_snapshot_for_recovery(self) -> None:
        created = operations.op_create(self.ws, "task", "Latest edit\n", title="Rollback Guard", sync=False)
        card = created.card
        assert card is not None
        original_text = card.path.read_text(encoding="utf-8")

        # Inject a failure after unlink (in validate_data_root) AND fail rollback (atomic_write).
        with mock.patch.object(transaction, "validate_data_root", side_effect=OSError("post-delete boom")), \
                mock.patch.object(transaction, "atomic_write", side_effect=OSError("rollback write failed")):
            with self.assertRaises(store.ValidationError):
                operations.op_delete(self.ws, f"@{card.short_ref}", by="root", sync=False)

        # Card was deleted and rollback failed, so the prepared recovery snapshot MUST remain intact.
        self.assertFalse(card.path.exists())
        deleted = history.list_events(self.ws, deleted_only=True)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["status"], "prepared")

        # Admin can still restore the card from that prepared snapshot.
        operations.op_restore(self.ws, deleted[0]["id"], by="root", sync=False)
        self.assertEqual(card.path.read_text(encoding="utf-8"), original_text)

    def test_forced_restore_requires_durable_snapshot_before_overwrite(self) -> None:
        created = operations.op_create(self.ws, "task", "v1\n", title="Force Guard", sync=False)
        card = created.card
        assert card is not None
        operations.op_edit(self.ws, f"@{card.short_ref}", body_mode="replace", body_text="v2-newer\n", by="alice", sync=False)
        edit_event = history.list_events(self.ws, limit=5)[0]
        v2_bytes = card.path.read_bytes()

        # Block snapshot writes during forced restore: newer v2 edit must NOT be overwritten.
        with mock.patch.object(history, "prepare_transaction", side_effect=store.HistoryRequiredError("db locked")):
            with self.assertRaises(store.HistoryRequiredError):
                operations.op_restore(self.ws, edit_event["id"], force=True, by="alice", sync=False)
        self.assertEqual(card.path.read_bytes(), v2_bytes)

    def test_cli_surfaces_history_warning_in_text_and_json(self) -> None:
        created = operations.op_create(self.ws, "task", "Body\n", title="CLI Warning", sync=False)
        card = created.card
        assert card is not None
        # Make history DB unreadable/unwritable by replacing it with a directory.
        db_path = self.ws.history_db_path()
        db_path.unlink(missing_ok=True)
        db_path.mkdir()

        text_res = self.mt("comment", f"@{card.short_ref}", "-m", "text warning")
        self.assertEqual(text_res.returncode, 0, text_res.stderr)
        self.assertIn("warning:", text_res.stderr)
        self.assertIn("Local history is unavailable", text_res.stderr)

        json_res = self.mt("comment", f"@{card.short_ref}", "-m", "json warning", "--json")
        self.assertEqual(json_res.returncode, 0, json_res.stderr)
        payload = json.loads(json_res.stdout)
        self.assertIn("Local history is unavailable", payload.get("history_warning", ""))

    def test_restore_from_multi_card_event_skips_diffs(self) -> None:
        c1 = operations.op_create(self.ws, "task", "One\n", title="T1", topic="old", sync=False).card
        c2 = operations.op_create(self.ws, "task", "Two\n", title="T2", topic="old", sync=False).card
        assert c1 is not None and c2 is not None
        operations.op_topic_rename(self.ws, "old", "new", by="root", sync=False)
        rename_event = history.list_events(self.ws, limit=5)[0]
        self.assertEqual(rename_event["status"], "committed")
        rel1 = str(c1.path.relative_to(self.root))

        with mock.patch.object(history.difflib, "unified_diff", side_effect=AssertionError("should not diff")):
            operations.op_restore(self.ws, rename_event["id"], path=rel1, force=True, by="root", sync=False)
        self.assertIn("topic: old", c1.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
