from __future__ import annotations

import argparse
import fcntl
import os
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
import tasklib  # noqa: E402


class TimeTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), *args],
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def add_task(self, title: str) -> Path:
        result = self.run_mt("add", title)
        self.assertEqual(result.returncode, 0, result.stderr)
        matches = list(self.root.glob(f"cards/*-{title.lower().replace(' ', '-')}.md"))
        self.assertEqual(len(matches), 1)
        return matches[0]

    def initialize_git(self) -> None:
        result = subprocess.run(
            ["git", "init", "-q"], cwd=self.root, capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @staticmethod
    def log_content(raw: str) -> str | None:
        return tasklib.time_log_entries(raw)

    def test_start_claims_and_switches_at_one_boundary_then_accumulates_sessions(self) -> None:
        task = self.add_task("Track focus session")
        other = self.add_task("Other running task")
        self.assertEqual(self.run_mt("time", "start", "Track focus session").returncode, 0)
        claimed = tasklib.Card.read(task)
        self.assertEqual(claimed.frontmatter["status"], "doing")
        self.assertTrue(claimed.frontmatter["claimed_by"])
        self.assertFalse((self.root / ".mt" / "focus").exists())
        state = self.root / ".mt" / "time-running"
        first_timer = json.loads(state.read_text())[task.stem]
        time.sleep(1.05)
        switched = self.run_mt("time", "start", "Other running task")
        self.assertEqual(switched.returncode, 0, switched.stderr)
        timers = json.loads(state.read_text())
        self.assertEqual(set(timers), {other.stem})
        entry = tasklib.time_log_entries(task.read_text())[0]
        self.assertEqual(entry["session_id"], first_timer["session_id"])
        self.assertEqual(entry["end"], timers[other.stem]["start"])
        self.assertEqual(entry["elapsed_seconds"], timers[other.stem]["start_epoch"] - first_timer["start_epoch"])
        duplicate = self.run_mt("time", "start", "Other running task")
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn("timer already running", duplicate.stderr)
        self.assertEqual(self.run_mt("time", "stop").returncode, 0)
        self.assertFalse(state.exists())
        self.assertEqual(self.run_mt("time", "start", "Track focus session").returncode, 0)
        self.assertEqual(self.run_mt("time", "stop").returncode, 0)
        entries = tasklib.time_log_entries(task.read_text())
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]["session_id"], entries[1]["session_id"])
        self.assertIn("Track focus session", self.run_mt("time", "report").stdout)

    def test_owners_switch_independently_and_stop_defaults_to_current_user(self) -> None:
        first = self.add_task("Alice first")
        second = self.add_task("Bob task")
        third = self.add_task("Alice next")
        for title, owner in (("Alice first", "alice"), ("Bob task", "bob"), ("Alice next", "alice")):
            result = self.run_mt("time", "start", title, "--by", owner)
            self.assertEqual(result.returncode, 0, result.stderr)
        state = self.root / ".mt/time-running"
        timers = json.loads(state.read_text())
        self.assertEqual(set(timers), {second.stem, third.stem})
        self.assertEqual(timers[second.stem]["by"], "bob")
        self.assertEqual(len(tasklib.time_log_entries(first.read_text())), 1)
        denied = self.run_mt("time", "stop", "Bob task", "--by", "alice")
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(state.read_text(), json.dumps(timers, indent=2) + "\n")
        self.assertEqual(self.run_mt("time", "stop", "--by", "alice").returncode, 0)
        self.assertEqual(set(json.loads(state.read_text())), {second.stem})
        self.assertEqual(self.run_mt("time", "cancel", "--by", "bob").returncode, 0)

    def test_orphan_status_stop_and_cancel_by_exact_id(self) -> None:
        task = self.add_task("Orphan timer")
        self.assertEqual(self.run_mt("time", "start", task.stem).returncode, 0)
        task.unlink()  # Simulate a deletion arriving from sync.
        status = self.run_mt("time", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("missing task " + task.stem, status.stdout)
        stopped = self.run_mt("time", "stop", task.stem)
        self.assertEqual(stopped.returncode, 1)
        self.assertIn("time cancel " + task.stem, stopped.stderr)
        self.assertEqual(self.run_mt("time", "cancel", task.stem).returncode, 0)
        self.assertFalse((self.root / ".mt/time-running").exists())

    def test_corruption_is_quarantined_and_durable_operations_continue(self) -> None:
        cases = ((("done",), b"{broken timer state"),
                 (("convert", "note"), b"[]"), (("delete",), b"\xff"))
        for command, corrupt in cases:
            with self.subTest(command=command):
                title = "Corrupt " + command[0]
                task = self.add_task(title)
                state = self.root / ".mt/time-running"
                state.parent.mkdir(exist_ok=True)
                state.write_bytes(corrupt)
                result = self.run_mt(command[0], title, *command[1:])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("warning: malformed running timer state moved", result.stderr)
                self.assertFalse(state.exists())
                self.assertTrue(any(p.read_bytes() == corrupt for p in (self.root / ".mt/recovery").iterdir()))

    def test_lifecycle_saves_timer_once_with_claim_changes(self) -> None:
        cases = (("move", "waiting"), ("edit", "--hidden"),
                 ("edit", "--archived"), ("edit", "--status", "active"),
                 ("move", "doing", "--by", "bob", "--takeover"))
        for index, command in enumerate(cases):
            with self.subTest(command=command):
                task = self.add_task(f"Lifecycle {index}")
                self.assertEqual(self.run_mt("time", "start", task.stem).returncode, 0)
                state = self.root / ".mt/time-running"
                timer = json.loads(state.read_text())[task.stem]
                result = self.run_mt(command[0], task.stem, *command[1:])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(state.exists())
                entries = tasklib.time_log_entries(task.read_text())
                self.assertEqual([entry["session_id"] for entry in entries], [timer["session_id"]])
                if "--takeover" in command:
                    self.assertEqual(tasklib.Card.read(task).frontmatter["claimed_by"], "bob")

    def test_failed_switch_and_lifecycle_keep_cards_and_timer(self) -> None:
        first = self.add_task("Switch rollback first")
        second = self.add_task("Switch rollback next")
        self.assertEqual(self.run_mt("time", "start", first.stem).returncode, 0)
        state = self.root / ".mt/time-running"
        before = {p: p.read_bytes() for p in (first, second, state)}
        with mock.patch.object(tasklib, "REPO_ROOT", self.root), \
             mock.patch.object(tasklib, "_DEMO_STATE", False), \
             mock.patch.object(tasklib, "run_validator", return_value=True), \
             mock.patch.object(tasklib, "validate_data_root", side_effect=ValueError("forced failure")):
            result = tasklib.command_time_start(argparse.Namespace(query=[second.stem]))
            self.assertEqual(result, 1)
            result = tasklib.command_move(argparse.Namespace(query=[first.stem], destination="waiting",
                                                            match_mode="exact", comments=[]))
            self.assertEqual(result, 1)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        (self.root / "mt-policy.local.yaml").write_text("rules:\n  max_doing_tasks: 1\n")
        rejected = self.run_mt("time", "start", second.stem)
        self.assertNotEqual(rejected.returncode, 0)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_bulk_lifecycle_edit_saves_time_and_releases_claim(self) -> None:
        task = self.add_task("Bulk timer lifecycle")
        self.assertEqual(self.run_mt("time", "start", task.stem).returncode, 0)
        preview = self.run_mt("bulk-edit", "--status", "doing", "--set", "status=waiting", "--json")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        plan = self.root / "plan.json"
        plan.write_text(preview.stdout)
        result = self.run_mt("bulk-edit", "--apply", str(plan), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        card = tasklib.Card.read(task)
        self.assertEqual(card.frontmatter["status"], "waiting")
        self.assertNotIn("claimed_by", card.frontmatter)
        self.assertEqual(len(tasklib.time_log_entries(card.raw)), 1)
        self.assertFalse((self.root / ".mt/time-running").exists())

    def test_failed_sidecar_write_rolls_back_switch(self) -> None:
        first = self.add_task("Sidecar switch first")
        second = self.add_task("Sidecar switch second")
        self.assertEqual(self.run_mt("time", "start", first.stem).returncode, 0)
        state = self.root / ".mt/time-running"
        before = {p: p.read_bytes() for p in (first, second, state)}
        with mock.patch.object(tasklib, "REPO_ROOT", self.root), \
             mock.patch.object(tasklib, "_DEMO_STATE", False), \
             mock.patch.object(tasklib, "run_validator", return_value=True), \
             mock.patch.object(tasklib, "write_time_timers", side_effect=OSError("forced failure")):
            self.assertEqual(tasklib.command_time_start(argparse.Namespace(query=[second.stem])), 1)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_legacy_owner_uses_claimant_and_is_persisted_on_read(self) -> None:
        task = self.add_task("Legacy claimant")
        self.assertEqual(self.run_mt("time", "start", task.stem, "--by", "alice").returncode, 0)
        state = self.root / ".mt/time-running"
        timers = json.loads(state.read_text())
        del timers[task.stem]["by"]
        state.write_text(json.dumps(timers))
        result = self.run_mt("time", "status", env={**os.environ, "MT_USERNAME": "bob"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alice", result.stdout)
        self.assertEqual(json.loads(state.read_text())[task.stem]["by"], "alice")

    def test_backdated_add_adjusted_stop_and_stale_warning(self) -> None:
        task = self.add_task("Forgotten timer")
        for args in (("--start", "2020-01-02T09:00", "--end", "2020-01-02T11:00"),
                     ("2h", "--end", "2020-01-03T11:00")):
            mutation_start = tasklib.now_stamp()
            result = self.run_mt("time", "add", task.stem, *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(tasklib.Card.read(task).frontmatter["updated"], mutation_start)
        entries = tasklib.time_log_entries(task.read_text())
        self.assertEqual([e["elapsed_seconds"] for e in entries], [7200, 7200])
        self.assertEqual(entries[0]["start"], "2020-01-02T09:00:00")
        self.assertEqual(entries[1]["start"], "2020-01-03T09:00:00")
        for flags in (("--duration", "2h"), ("--at", "2020-01-04T11:00")):
            self.assertEqual(self.run_mt("time", "start", task.stem).returncode, 0)
            state = self.root / ".mt/time-running"
            timers = json.loads(state.read_text())
            timers[task.stem].update(start="2020-01-04T09:00:00",
                                    start_epoch=int(tasklib.datetime(2020, 1, 4, 9).timestamp()))
            state.write_text(json.dumps(timers))
            self.assertIn("long-running timer", self.run_mt("time", "status").stdout)
            mutation_start = tasklib.now_stamp()
            result = self.run_mt("time", "stop", task.stem, *flags)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreaterEqual(tasklib.Card.read(task).frontmatter["updated"], mutation_start)
            self.assertEqual(tasklib.time_log_entries(task.read_text())[-1]["elapsed_seconds"], 7200)
        for flags in (("--start", "2020-01-02T11:00", "--end", "2020-01-02T09:00"),
                      ("--start", "2020-01-02T09:00Z"), ("2h", "--start", "2020-01-02T09:00")):
            before = task.read_bytes()
            result = self.run_mt("time", "add", task.stem, *flags)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(task.read_bytes(), before)
        self.assertEqual(self.run_mt("time", "start", task.stem).returncode, 0)
        before = task.read_bytes()
        for flags in (("--duration", "2h"), ("--at", "2020-01-01T00:00"), ("--at", "bad")):
            self.assertNotEqual(self.run_mt("time", "stop", task.stem, *flags).returncode, 0)
            self.assertEqual(task.read_bytes(), before)
            self.assertTrue((self.root / ".mt/time-running").exists())
        policy = self.run_mt("policy", "--json")
        self.assertEqual(float(json.loads(policy.stdout)["rules"]["timer_warn_hours"]), 8)

    def test_backdated_session_on_completed_task_preserves_completion_and_recent_update(self) -> None:
        task = self.add_task("Backdate completed work")
        self.assertEqual(self.run_mt("done", task.stem).returncode, 0)
        completed = tasklib.Card.read(task).frontmatter["completed"]
        mutation_start = tasklib.now_stamp()
        result = self.run_mt("time", "add", task.stem,
                             "--start", "2020-01-02T09:00", "--end", "2020-01-02T10:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        card = tasklib.Card.read(task)
        self.assertEqual(card.frontmatter["completed"], completed)
        self.assertGreaterEqual(card.frontmatter["updated"], mutation_start)
        self.assertGreaterEqual(card.frontmatter["updated"], completed)
        entry = tasklib.time_log_entries(card.raw)[0]
        self.assertEqual(entry["start"], "2020-01-02T09:00:00")
        self.assertEqual(entry["end"], "2020-01-02T10:00:00")
        self.assertEqual(entry["elapsed_seconds"], 3600)

    def test_cancel_discards_only_selected_timer_and_never_changes_cards(self) -> None:
        first = self.add_task("Cancel forgotten timer")
        second = self.add_task("Keep other timer")
        self.assertEqual(self.run_mt("time", "add", "Cancel forgotten timer", "5m").returncode, 0)
        for title in ("Cancel forgotten timer", "Keep other timer"):
            self.assertEqual(self.run_mt("time", "start", title, "--parallel").returncode, 0)
        state = self.root / ".mt" / "time-running"
        before = {path: path.read_bytes() for path in (first, second)}
        timers = state.read_bytes()
        ambiguous = self.run_mt("time", "cancel")
        self.assertNotEqual(ambiguous.returncode, 0)
        self.assertEqual(state.read_bytes(), timers)
        cancelled = self.run_mt("time", "cancel", "Cancel forgotten timer")
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(set(json.loads(state.read_text())), {second.stem})
        missing = self.run_mt("time", "cancel", "Cancel forgotten timer")
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.run_mt("time", "cancel").returncode, 0)
        self.assertFalse(state.exists())
        self.assertNotEqual(self.run_mt("time", "cancel").returncode, 0)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_list_and_remove_sessions_preserve_other_content_and_running_timers(self) -> None:
        task = self.add_task("Correct recorded time")
        other = self.add_task("Keep recorded time")
        for title, duration in (("Correct recorded time", "5m"), ("Correct recorded time", "10m"),
                                ("Keep recorded time", "15m")):
            result = self.run_mt("time", "add", title, duration)
            self.assertEqual(result.returncode, 0, result.stderr)
        entries = tasklib.time_log_entries(task.read_text())
        first, second = entries
        # A quoted session in Notes must survive removal from Time log.
        quoted = f"session {first['session_id']} | start: {first['start']} | end: {first['end']}"
        fields, body = tasklib.card_format.parse_document(task.read_text())
        raw = tasklib.card_format.render_document(fields, body + "\n\n## Notes\n\n" + quoted + "\n")
        raw = tasklib.replace_frontmatter_field(raw, "updated", "2000-01-01T00:00:00")
        task.write_text(raw)
        original = task.read_text()
        other_before = other.read_bytes()
        self.assertEqual(self.run_mt("time", "start", "Correct recorded time").returncode, 0)
        state = self.root / ".mt" / "time-running"
        timers_before = state.read_bytes()

        listed = self.run_mt("time", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(str(first["session_id"])[:8], listed.stdout)
        self.assertIn("Keep recorded time", listed.stdout)
        filtered = self.run_mt("time", "list", "Correct recorded time")
        self.assertEqual(filtered.returncode, 0, filtered.stderr)
        self.assertNotIn("Keep recorded time", filtered.stdout)
        self.assertIn("10m", filtered.stdout)

        removed = self.run_mt("time", "remove", str(first["session_id"])[:8])
        self.assertEqual(removed.returncode, 0, removed.stderr)
        after = task.read_text()
        self.assertEqual(tasklib.time_log_entries(after), [second])
        self.assertEqual(tasklib.card_format.parse_document(after)[1], tasklib.card_format.parse_document(original)[1])
        self.assertNotIn("updated: 2000-01-01T00:00:00", after)
        self.assertEqual(other.read_bytes(), other_before)
        self.assertEqual(state.read_bytes(), timers_before)
        self.assertEqual(self.run_mt("time", "remove", str(second["session_id"])).returncode, 0)
        self.assertFalse(tasklib.time_log_entries(task.read_text()))
        self.assertEqual(self.run_mt("time", "report").stdout.strip(), "[@" + tasklib.Card.read(other).short_ref + "] Keep recorded time: 15m\nTotal: 15m")
        self.assertIn("No recorded sessions", self.run_mt("time", "list", "Correct recorded time").stdout)

    def test_remove_rejects_unknown_and_ambiguous_session_ids_without_changes(self) -> None:
        task = self.add_task("Session collisions")
        ids = ("abcdef12" + "0" * 24, "abcdef12" + "1" * 24)
        raw = task.read_text()
        for session_id in ids:
            raw = tasklib.append_time_entry(raw, {
                "session_id": session_id, "start": "2026-01-01T10:00:00",
                "end": "2026-01-01T10:05:00", "elapsed_seconds": 300,
            })
        task.write_text(raw)
        before = task.read_bytes()
        for ref in ("abcdef12", "deadbeef", "abc", "not-a-session"):
            with self.subTest(ref=ref):
                rejected = self.run_mt("time", "remove", ref)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(task.read_bytes(), before)
        self.assertEqual(self.run_mt("time", "remove", ids[0]).returncode, 0)
        self.assertEqual([entry["session_id"] for entry in tasklib.time_log_entries(task.read_text())], [ids[1]])

    def test_failed_session_removal_restores_original_task(self) -> None:
        task = self.add_task("Removal validation failure")
        self.assertEqual(self.run_mt("time", "add", "Removal validation failure", "5m").returncode, 0)
        session_id = str(tasklib.time_log_entries(task.read_text())[0]["session_id"])
        original = task.read_bytes()
        with mock.patch.object(tasklib, "REPO_ROOT", self.root), \
             mock.patch.object(tasklib, "run_validator", return_value=True), \
             mock.patch.object(tasklib, "validate_data_root", side_effect=ValueError("forced post-write validation failure")), \
             mock.patch("builtins.print"):
            result = tasklib.command_time_remove(argparse.Namespace(session_id=session_id))
        self.assertEqual(result, 1)
        self.assertEqual(task.read_bytes(), original)

    def test_lock_files_stay_in_repo_state_dir_across_demo_transition(self) -> None:
        effective_policy = copy.deepcopy(tasklib.policy())

        def validate_while_locked() -> bool:
            with (self.root / ".mt" / "time-mutation.lock").open("a+") as lock_file:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True

        timer = {
            "demo-task": {
                "session_id": "a" * 32,
                "start": "2026-09-25T12:00:00",
                "start_epoch": 1790337600,
            }
        }
        with mock.patch.object(tasklib, "REPO_ROOT", self.root), mock.patch.object(
            tasklib, "APP_ROOT", self.root
        ), mock.patch.object(
            tasklib, "_DEMO_STATE", None
        ), mock.patch.object(tasklib, "policy", return_value=effective_policy), mock.patch.object(
            tasklib, "run_validator", side_effect=validate_while_locked
        ):
            self.assertTrue(tasklib.demo_active())
            demo_timer_path = tasklib.time_timer_path()
            self.assertEqual(demo_timer_path, self.root / ".mt" / "demo" / "time-running")
            tasklib.write_time_timers(timer)
            with tasklib.time_mutation_lock():
                pass
            with tasklib.sync_serialization_lock():
                pass

            created = tasklib.command_create(type("Args", (), {"text": ["First real task"]})())

            self.assertEqual(created, 0)
            self.assertFalse(tasklib.demo_active())
            self.assertEqual(tasklib.time_timer_path(), self.root / ".mt" / "time-running")
            with tasklib.time_mutation_lock():
                pass
            with tasklib.sync_serialization_lock():
                pass

        lock_dir = self.root / ".mt"
        self.assertTrue((lock_dir / "time-mutation.lock").is_file())
        self.assertTrue((lock_dir / "sync.lock").is_file())
        self.assertFalse((lock_dir / "demo" / "time-mutation.lock").exists())
        self.assertFalse((lock_dir / "demo" / "sync.lock").exists())
        self.assertTrue(demo_timer_path.is_file())
        self.assertFalse((lock_dir / "time-running").exists())

    def test_manual_session_reports_by_project_and_topic(self) -> None:
        task = self.add_task("Track manual session")
        edited = self.run_mt(
            "edit", "Track manual session", "--set", "project=Atlas", "--set", "topic=learning"
        )
        self.assertEqual(edited.returncode, 0, edited.stderr)

        added = self.run_mt("time", "add", "Track manual session", "1h30m")

        self.assertEqual(added.returncode, 0, added.stderr)
        raw = task.read_text(encoding="utf-8")
        self.assertIn("elapsed_seconds: 5400", raw)
        self.assertIn("start: ", raw)
        self.assertIn("end: ", raw)
        self.assertIn("Atlas: 1h 30m", self.run_mt("time", "report", "--by", "project").stdout)
        self.assertIn("learning: 1h 30m", self.run_mt("time", "report", "--by", "topic").stdout)

    def test_appending_time_preserves_existing_body_whitespace_verbatim(self) -> None:
        task = self.add_task("Preserve time log whitespace")
        fields, _ = tasklib.card_format.parse_document(task.read_text())
        body = "  \r\n## Time log\r\nLiteral document content.\r\n\r\n```python\r\nunfinished"
        task.write_bytes(tasklib.card_format.render_document(fields, body).encode())
        for duration in ("5m", "2m"):
            result = self.run_mt("time", "add", task.stem, duration)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(tasklib.card_format.parse_document(task.read_bytes().decode())[1], body)
        self.assertEqual([entry["elapsed_seconds"] for entry in tasklib.time_log_entries(task.read_bytes().decode())], [300, 120])

    def test_manual_entry_uses_elapsed_epochs_across_dst(self) -> None:
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            entry = tasklib.time_entry_from_timer(
                {
                    "session_id": "a" * 32,
                    "start": tasklib.local_stamp(1710052200),
                    "start_epoch": 1710052200,
                },
                1710055800,
            )
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()
        self.assertEqual(entry["start"], "2024-03-10T01:30:00")
        self.assertEqual(entry["end"], "2024-03-10T03:30:00")
        self.assertEqual(entry["elapsed_seconds"], 3600)

    def test_timer_can_stop_after_workspace_timezone_changes(self) -> None:
        task = self.add_task("Stop after timezone change")
        start_env = os.environ.copy()
        start_env["TZ"] = "America/New_York"
        stop_env = os.environ.copy()
        stop_env["TZ"] = "America/Los_Angeles"

        started = self.run_mt("time", "start", "Stop after timezone change", env=start_env)
        self.assertEqual(started.returncode, 0, started.stderr)
        state = json.loads((self.root / ".mt" / "time-running").read_text(encoding="utf-8"))
        time.sleep(1.05)

        stopped = self.run_mt("time", "stop", env=stop_env)

        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        entry = tasklib.time_log_entries(task.read_text())[0]
        self.assertEqual(entry["start"], state[task.stem]["start"])
        self.assertGreaterEqual(entry["elapsed_seconds"], 1)
        self.assertFalse((self.root / ".mt" / "time-running").exists())

    def test_retry_after_saved_session_does_not_duplicate_it(self) -> None:
        task = self.add_task("Retry timer stop")
        self.assertEqual(self.run_mt("time", "start", "Retry timer stop").returncode, 0)
        state = self.root / ".mt" / "time-running"
        original_state = state.read_bytes()
        first_stop = self.run_mt("time", "stop")
        self.assertEqual(first_stop.returncode, 0, first_stop.stderr)
        state.write_bytes(original_state)

        retry = self.run_mt("time", "stop")

        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("already saved", retry.stdout)
        raw = task.read_text(encoding="utf-8")
        self.assertEqual(len(tasklib.time_log_entries(raw)), 1)
        self.assertFalse(state.exists())

    def test_stop_without_running_timer_fails_clearly(self) -> None:
        stopped = self.run_mt("time", "stop")

        self.assertEqual(stopped.returncode, 1)
        self.assertIn("no timer is running", stopped.stderr)

    def test_saved_session_syncs_even_when_timer_cleanup_fails(self) -> None:
        self.initialize_git()
        effective_policy = copy.deepcopy(tasklib.policy())
        effective_policy.setdefault("rules", {})["auto_sync"] = True
        for cleanup_result in (False, OSError("timer cleanup failed")):
            with self.subTest(cleanup_result=cleanup_result):
                task = self.add_task("Cleanup failure " + str(isinstance(cleanup_result, OSError)))
                self.assertEqual(self.run_mt("time", "start", task.stem).returncode, 0)

                def verify_sync(*_args: object, **_kwargs: object) -> int:
                    self.assertFalse(getattr(tasklib._TIME_LOCK_STATE, "held", False))
                    self.assertIn("time_entries:", task.read_text())
                    return 0

                args = argparse.Namespace(query=[task.stem], time_command="stop")
                with (
                    mock.patch.object(tasklib, "REPO_ROOT", self.root),
                    mock.patch.object(tasklib, "_DEMO_STATE", False),
                    mock.patch.object(tasklib, "policy", return_value=effective_policy),
                    mock.patch.object(tasklib, "clear_time_timer", **(
                        {"side_effect": cleanup_result} if isinstance(cleanup_result, OSError)
                        else {"return_value": cleanup_result}
                    )),
                    mock.patch.object(tasklib, "command_sync", side_effect=verify_sync) as sync,
                ):
                    self.assertEqual(tasklib.command_time_end(args), 1)
                    sync.assert_called_once()
                self.assertTrue((self.root / ".mt" / "time-running").is_file())
                retried = self.run_mt("time", "stop", task.stem)
                self.assertEqual(retried.returncode, 0, retried.stderr)
                self.assertEqual(len(tasklib.time_log_entries(task.read_text())), 1)

    def test_existing_timer_is_kept_when_another_task_timer_starts(self) -> None:
        first = self.add_task("Legacy timer")
        second = self.add_task("New timer")
        start_epoch = int(time.time())
        state = self.root / ".mt" / "time-running"
        state.parent.mkdir(parents=True, exist_ok=True)
        legacy = {
            "session_id": "a" * 32,
            "task_id": first.stem,
            "start": tasklib.local_stamp(start_epoch),
            "start_epoch": start_epoch,
        }
        state.write_text(json.dumps({first.stem: {key: value for key, value in legacy.items() if key != "task_id"}}) + "\n", encoding="utf-8")

        status = self.run_mt("time", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("Legacy timer", status.stdout)
        started = self.run_mt("time", "start", "New timer", "--parallel")

        self.assertEqual(started.returncode, 0, started.stderr)
        timers = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual({key: value for key, value in timers[first.stem].items() if key != "by"},
                         {key: value for key, value in legacy.items() if key != "task_id"})
        self.assertTrue(timers[first.stem]["by"])
        self.assertEqual(set(timers[second.stem]), {"session_id", "start", "start_epoch", "by"})
        self.assertEqual(self.run_mt("time", "stop", "Legacy timer").returncode, 0)
        self.assertEqual(self.run_mt("time", "stop", "New timer").returncode, 0)

    def test_task_scoped_stop_leaves_other_task_timer_running(self) -> None:
        first = self.add_task("First task timer")
        second = self.add_task("Second task timer")
        self.assertEqual(self.run_mt("time", "start", "First task timer").returncode, 0)
        self.assertEqual(self.run_mt("time", "start", "Second task timer", "--parallel").returncode, 0)

        stopped = self.run_mt("time", "stop", "First task timer")

        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        state = json.loads((self.root / ".mt" / "time-running").read_text(encoding="utf-8"))
        self.assertEqual(set(state), {second.stem})
        missing = self.run_mt("time", "stop", "First task timer")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no timer is running for", missing.stderr)
        self.assertEqual(self.run_mt("time", "stop", "Second task timer").returncode, 0)

    def test_converting_untimed_task_does_not_conflict_with_other_task_timer(self) -> None:
        timed = self.add_task("Task with timer")
        untimed = self.add_task("Untimed conversion")
        self.assertEqual(self.run_mt("time", "start", "Task with timer").returncode, 0)

        converted = self.run_mt("convert", "Untimed conversion", "note")

        self.assertEqual(converted.returncode, 0, converted.stderr)
        self.assertIn("kind: note", untimed.read_text(encoding="utf-8"))
        self.assertEqual(set(json.loads((self.root / ".mt" / "time-running").read_text(encoding="utf-8"))), {timed.stem})
        self.assertEqual(self.run_mt("time", "stop", "Task with timer").returncode, 0)

    def test_delete_is_blocked_while_task_timer_is_running(self) -> None:
        task = self.add_task("Keep timed task")
        self.assertEqual(self.run_mt("time", "start", "Keep timed task").returncode, 0)

        deleted = self.run_mt("delete", "Keep timed task")

        self.assertEqual(deleted.returncode, 1)
        self.assertIn("running timer cannot be deleted", deleted.stderr)
        self.assertTrue(task.is_file())
        self.assertTrue((self.root / ".mt" / "time-running").is_file())
        self.assertEqual(self.run_mt("time", "stop").returncode, 0)

    def test_move_and_time_stop_serialize_without_losing_the_session(self) -> None:
        task = self.add_task("Move during timer stop")
        self.assertEqual(self.run_mt("time", "start", "Move during timer stop").returncode, 0)
        status_ready = threading.Event()
        continue_move = threading.Event()
        original_replace = tasklib.replace_frontmatter_field

        def pause_after_status(raw: str, field: str, value: str) -> str:
            updated = original_replace(raw, field, value)
            if field == "status" and value == "waiting":
                status_ready.set()
                if not continue_move.wait(timeout=10):
                    raise TimeoutError("timed out waiting to finish the staged move")
            return updated

        move_args = type(
            "MoveArgs",
            (),
            {"query": ["Move during timer stop"], "destination": "waiting", "match_mode": "exact", "comments": []},
        )()
        with mock.patch.object(tasklib, "REPO_ROOT", self.root), mock.patch.object(
            tasklib, "_DEMO_STATE", False
        ), mock.patch.object(tasklib, "replace_frontmatter_field", side_effect=pause_after_status):
            with ThreadPoolExecutor(max_workers=2) as executor:
                move_future = executor.submit(tasklib.command_move, move_args)
                self.assertTrue(status_ready.wait(timeout=10))
                stop_future = executor.submit(self.run_mt, "time", "stop")
                time.sleep(0.2)
                self.assertFalse(stop_future.done(), "time stop should wait for the move's lock")
                continue_move.set()
                self.assertEqual(move_future.result(timeout=10), 0)
                stopped = stop_future.result(timeout=10)

        self.assertEqual(stopped.returncode, 1, stopped.stderr)
        self.assertIn("no timer is running", stopped.stderr)
        moved = task
        self.assertTrue(moved.is_file())
        self.assertEqual(len(tasklib.time_log_entries(moved.read_text())), 1)

    def test_time_lock_is_released_before_auto_sync(self) -> None:
        self.initialize_git()
        self.add_task("Add before sync")
        self.add_task("Start during sync")
        sync_entered = threading.Event()
        release_sync = threading.Event()
        effective_policy = copy.deepcopy(tasklib.policy())
        effective_policy.setdefault("rules", {})["auto_sync"] = True

        def wait_for_sync(*_args: object, **_kwargs: object) -> int:
            sync_entered.set()
            if not release_sync.wait(timeout=10):
                raise TimeoutError("timed out waiting to release mocked sync")
            return 0

        args = type("TimeAddArgs", (), {"query": "Add before sync", "duration": "5m"})()
        with mock.patch.object(tasklib, "REPO_ROOT", self.root), mock.patch.object(
            tasklib, "_DEMO_STATE", False
        ), mock.patch.object(tasklib, "policy", return_value=effective_policy), mock.patch.object(
            tasklib, "command_sync", side_effect=wait_for_sync
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                add_future = executor.submit(tasklib.command_time_add, args)
                self.assertTrue(sync_entered.wait(timeout=5), "auto-sync was not reached")
                start_future = executor.submit(self.run_mt, "time", "start", "Start during sync")
                try:
                    started = start_future.result(timeout=2)
                except TimeoutError:
                    self.fail("time start remained blocked while auto-sync was running")
                finally:
                    release_sync.set()
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertEqual(add_future.result(timeout=5), 0)

        self.assertEqual(self.run_mt("time", "stop").returncode, 0)



    def test_concurrent_starts_keep_one_running_timer_per_task(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text("rules:\n  max_doing_tasks: 10\n")
        titles = [f"Concurrent start {index}" for index in range(8)]
        tasks = {title: self.add_task(title) for title in titles}
        barrier = threading.Barrier(len(titles))

        def start(title: str) -> subprocess.CompletedProcess[str]:
            barrier.wait(timeout=10)
            return self.run_mt("time", "start", title, "--parallel")

        with ThreadPoolExecutor(max_workers=len(titles)) as executor:
            results = list(executor.map(start, titles))

        self.assertTrue(all(result.returncode == 0 for result in results), [r.stderr for r in results])
        state = json.loads((self.root / ".mt" / "time-running").read_text(encoding="utf-8"))
        self.assertEqual(set(state), {task.stem for task in tasks.values()})
        for title in titles:
            stopped = self.run_mt("time", "stop", title)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertFalse((self.root / ".mt" / "time-running").exists())

    def test_concurrent_same_task_start_and_different_task_stops(self) -> None:
        first = self.add_task("Contended timer task")
        second = self.add_task("Other timer task")
        start_barrier = threading.Barrier(2)

        def start_same_task() -> subprocess.CompletedProcess[str]:
            start_barrier.wait(timeout=10)
            return self.run_mt("time", "start", "Contended timer task")

        with ThreadPoolExecutor(max_workers=2) as executor:
            start_futures = [executor.submit(start_same_task) for _ in range(2)]
            starts = [future.result() for future in start_futures]

        self.assertTrue(all(result.returncode in (0, 1) for result in starts), [r.stderr for r in starts])
        self.assertEqual(sum(result.returncode == 0 for result in starts), 1, [r.stderr for r in starts])
        self.assertEqual(sum("timer already running" in result.stderr for result in starts), 1)
        self.assertEqual(self.run_mt("time", "start", "Other timer task", "--parallel").returncode, 0)

        stop_barrier = threading.Barrier(2)

        def stop_task(title: str) -> subprocess.CompletedProcess[str]:
            stop_barrier.wait(timeout=10)
            return self.run_mt("time", "stop", title)

        with ThreadPoolExecutor(max_workers=2) as executor:
            stops = list(executor.map(stop_task, ("Contended timer task", "Other timer task")))

        self.assertTrue(all(result.returncode == 0 for result in stops), [r.stderr for r in stops])
        for task in (first, second):
            self.assertEqual(
                len(tasklib.time_log_entries(task.read_text())),
                1,
            )
        self.assertFalse((self.root / ".mt" / "time-running").exists())

    def test_concurrent_stop_and_manual_add_preserve_both_sessions(self) -> None:
        task = self.add_task("Concurrent save sessions")
        self.assertEqual(self.run_mt("time", "start", "Concurrent save sessions").returncode, 0)
        barrier = threading.Barrier(2)

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            barrier.wait(timeout=10)
            return self.run_mt(*args)

        with ThreadPoolExecutor(max_workers=2) as executor:
            stop_result = executor.submit(run, "time", "stop")
            add_result = executor.submit(run, "time", "add", "Concurrent save sessions", "2m")
            results = [stop_result.result(), add_result.result()]

        self.assertTrue(all(result.returncode == 0 for result in results), [r.stderr for r in results])
        raw = task.read_text(encoding="utf-8")
        entries = tasklib.time_log_entries(raw)
        self.assertEqual(len(entries), 2, raw)
        self.assertEqual(len({entry["session_id"] for entry in entries}), 2)
        self.assertGreaterEqual(sum(entry["elapsed_seconds"] for entry in entries), 120)

    def test_failed_save_restores_task_and_retains_running_state(self) -> None:
        task = self.add_task("Timer save failure")
        other = self.add_task("Existing session ID")
        added = self.run_mt("time", "add", "Existing session ID", "5m")
        self.assertEqual(added.returncode, 0, added.stderr)
        session_id = tasklib.time_log_entries(other.read_text())[0]["session_id"]
        start_epoch = int(time.time())
        state = self.root / ".mt" / "time-running"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {task.stem: {
                    "session_id": session_id,
                    "start": tasklib.local_stamp(start_epoch),
                    "start_epoch": start_epoch,
                }}
            )
            + "\n",
            encoding="utf-8",
        )
        original = task.read_bytes()

        stopped = self.run_mt("time", "stop")

        self.assertEqual(stopped.returncode, 1)
        self.assertIn("running timer was retained", stopped.stderr)
        self.assertEqual(task.read_bytes(), original)
        self.assertTrue(state.is_file())

    def test_completion_stops_timer_and_preserves_saved_log(self) -> None:
        task = self.add_task("Finish timed task")
        other = self.add_task("Leave unrelated timer running")
        self.assertEqual(self.run_mt("time", "add", "Finish timed task", "12m").returncode, 0)
        prior_log = self.log_content(task.read_text(encoding="utf-8"))
        self.assertEqual(self.run_mt("time", "start", "Finish timed task").returncode, 0)
        self.assertEqual(self.run_mt("time", "start", "Leave unrelated timer running", "--parallel").returncode, 0)

        completed = self.run_mt("done", "Finish timed task")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        done_task = task
        raw = done_task.read_text(encoding="utf-8")
        timers = json.loads((self.root / ".mt" / "time-running").read_text(encoding="utf-8"))
        self.assertEqual(set(timers), {other.stem})
        self.assertEqual(self.log_content(raw)[:len(prior_log)], prior_log)
        entries = tasklib.time_log_entries(raw)
        self.assertEqual(len(entries), 2)

        self.assertEqual(self.run_mt("time", "stop", "Leave unrelated timer running").returncode, 0)

    def test_conversion_and_invalid_time_logs_are_rejected(self) -> None:
        task = self.add_task("Convert tracked task")
        self.assertEqual(self.run_mt("time", "add", "Convert tracked task", "5m").returncode, 0)
        converted = self.run_mt("convert", "Convert tracked task", "note")
        self.assertEqual(converted.returncode, 1)
        self.assertIn("cannot be converted", converted.stderr)
        self.assertTrue(task.is_file())

        raw = task.read_text(encoding="utf-8").replace(
            "elapsed_seconds: 300", "elapsed_seconds: nope"
        )
        task.write_text(raw, encoding="utf-8")
        validation = self.run_mt("validate")
        self.assertNotEqual(validation.returncode, 0)
        self.assertIn("elapsed_seconds must be a non-negative integer", validation.stderr)

    def test_body_time_log_headings_are_ordinary_content_but_note_records_are_rejected(self) -> None:
        result = self.run_mt("note", "A reference note")
        self.assertEqual(result.returncode, 0, result.stderr)
        note = next(self.root.glob("cards/*a-reference-note.md"))
        note.write_text(note.read_text() + "\n## Time log\n\n## Comments\n\nDocument headings.")
        self.assertEqual(self.run_mt("validate").returncode, 0)
        fields, body = tasklib.card_format.parse_document(note.read_text())
        fields["time_entries"] = []
        # External editors may introduce invalid properties; validator must diagnose them.
        note.write_text("---\n" + tasklib.card_format.dump_properties(fields) + "---\n" + body)
        rejected = self.run_mt("validate")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("time_entries", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
