from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402
import tasklib


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name)
        self.root = temporary / "repo"
        self.origin = temporary / "origin.git"
        shutil.copytree(
            SOURCE_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"),
        )
        self.git(self.origin.parent, "init", "--bare", str(self.origin))
        self.git(self.root, "init", "-b", "main")
        self.git(self.root, "config", "user.name", "Sync Test")
        self.git(self.root, "config", "user.email", "sync@example.invalid")
        self.git(self.root, "remote", "add", "origin", str(self.origin))
        self.git(self.root, "add", ".")
        self.git(self.root, "commit", "-m", "Initial")
        self.git(self.root, "push", "-u", "origin", "main")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def run_mt(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "mt"), *args],
            cwd=self.root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

    def enable_auto_sync(self) -> None:
        policy = self.root / "mt-policy.yaml"
        policy.write_text(
            policy.read_text(encoding="utf-8").replace("auto_sync: false", "auto_sync: true"),
            encoding="utf-8",
        )
        self.git(self.root, "add", "mt-policy.yaml")
        self.git(self.root, "commit", "-m", "Enable auto sync")

    def add_task(self, title: str = "Call dentist") -> Path:
        result = self.run_mt("add", title)
        self.assertEqual(result.returncode, 0, result.stderr)
        return card_files(self.root, "task")[0]

    def test_default_add_leaves_task_local(self) -> None:
        before = self.git(self.root, "rev-parse", "HEAD").stdout.strip()

        task = self.add_task("Keep local")
        after = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        remote = self.git(
            self.root,
            "--git-dir",
            str(self.origin),
            "rev-parse",
            "refs/heads/main",
        ).stdout.strip()

        self.assertEqual(after, before)
        self.assertEqual(remote, before)
        self.assertTrue(task.is_file())

    def test_auto_sync_add_commits_and_pushes_created_task(self) -> None:
        self.enable_auto_sync()

        result = self.run_mt("add", "Publish this task")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Auto-syncing task changes", result.stdout)
        message = self.git(self.root, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(message, "Add task Publish this task")
        remote_head = self.git(
            self.root,
            "--git-dir",
            str(self.origin),
            "rev-parse",
            "refs/heads/main",
        ).stdout.strip()
        local_head = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(remote_head, local_head)

    def test_auto_sync_runs_after_comment_move_edit_and_done(self) -> None:
        self.enable_auto_sync()
        task = self.add_task("Sync lifecycle")

        comment = self.run_mt("comment", "Sync lifecycle", "-m", "First note")
        self.assertEqual(comment.returncode, 0, comment.stderr)
        self.assertEqual(self.git(self.root, "log", "-1", "--format=%s").stdout.strip(), "Update task Sync lifecycle")

        move = self.run_mt("move", "Sync lifecycle", "waiting")
        self.assertEqual(move.returncode, 0, move.stderr)
        self.assertEqual(self.git(self.root, "log", "-1", "--format=%s").stdout.strip(), "Move task Sync lifecycle to waiting")

        edit = self.run_mt("edit", "Sync lifecycle", "--set", "priority=high")
        self.assertEqual(edit.returncode, 0, edit.stderr)
        self.assertEqual(self.git(self.root, "log", "-1", "--format=%s").stdout.strip(), "Update task Sync lifecycle")

        done = self.run_mt("done", "Sync lifecycle")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.git(self.root, "log", "-1", "--format=%s").stdout.strip(), "Complete task Sync lifecycle")
        self.assertTrue((self.root / "cards" / task.name).is_file())

    def test_auto_sync_skips_noop_move(self) -> None:
        self.enable_auto_sync()
        self.add_task("Noop task")
        before = self.git(self.root, "rev-parse", "HEAD").stdout.strip()

        result = self.run_mt("move", "Noop task", "inbox")
        after = self.git(self.root, "rev-parse", "HEAD").stdout.strip()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Auto-syncing task changes", result.stdout)
        self.assertEqual(after, before)

    def assert_published_mutation(self, *args: str, env: dict[str, str] | None = None) -> None:
        before = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        result = self.run_mt(*args, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        local = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        remote = self.git(self.root, "--git-dir", str(self.origin), "rev-parse", "refs/heads/main").stdout.strip()
        self.assertNotEqual(before, local, args)
        self.assertEqual(local, remote, args)
        self.assertEqual(self.git(self.root, "status", "--porcelain").stdout, "", args)

    def test_auto_sync_publishes_note_edits_conversions_and_deletion(self) -> None:
        self.enable_auto_sync()
        self.assert_published_mutation("note", "Research reference")
        note = card_files(self.root, "note")[0]
        self.assert_published_mutation("edit", note.stem, "--set", "hidden=true")
        self.assert_published_mutation("edit", note.stem, "--set", "hidden=false")
        body = Path(self.temporary.name) / "body.md"
        body.write_text("### Findings\n\nVerified evidence.\n")
        snapshot = tasklib.Card.read(note)
        self.assert_published_mutation("edit", note.stem, "--body", "--file", str(body), "--expected-hash", tasklib.card_format.content_hash(snapshot.raw))
        body.write_text("Additional context.\n")
        self.assert_published_mutation("edit", note.stem, "--body", "--append", "--file", str(body))
        self.assert_published_mutation("edit", note.stem, "--set", "priority=high")
        self.assert_published_mutation("edit", note.stem, "--set", "archived=true")
        self.assert_published_mutation("edit", note.stem, "--set", "archived=false")
        self.assert_published_mutation("comment", note.stem, "-m", "Discussed findings.")

        self.assert_published_mutation("edit", note.stem, "--title", "Revised reference")
        self.assertEqual(tasklib.parse_yaml(tasklib.parse_frontmatter(self.git(self.root, "show", f"origin/main:cards/{note.name}").stdout)[0])["title"], "Revised reference")
        self.assert_published_mutation("convert", note.stem, "task")
        self.assertIn("kind: task", note.read_text(encoding="utf-8"))
        self.assert_published_mutation("convert", note.stem, "note")
        self.assertTrue(note.exists())
        self.assert_published_mutation("delete", note.stem)
        self.assertFalse(note.exists())
        self.assertEqual(self.git(self.root, "ls-tree", "--name-only", "origin/main", "--", f"cards/{note.name}").stdout, "")

    def test_auto_sync_publishes_claims_and_time_but_skips_local_state(self) -> None:
        self.enable_auto_sync()
        task = self.add_task("Coordinated work")
        body = Path(self.temporary.name) / "action.md"
        body.write_text("Verify the result.\n")
        self.assert_published_mutation("edit", task.stem, "--set", "next_action=Verify the result.")
        self.assert_published_mutation("move", task.stem, "doing", "--by", "agent:test")
        doing = self.root / "cards" / task.name
        frontmatter, _body = tasklib.parse_frontmatter(doing.read_text())
        self.assertEqual(tasklib.parse_yaml(frontmatter)["claimed_by"], "agent:test")

        before = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        original = doing.read_bytes()
        current_priority = str(tasklib.Card.read(doing).frontmatter["priority"])
        for args in (
            ("read", task.stem), ("list", "--json"),
            ("focus", task.stem, "--by", "agent:test"), ("unfocus",),
            ("edit", task.stem, "--set", f"priority={current_priority}", "--by", "agent:test"),
            ("move", task.stem, "doing", "--by", "agent:test"),
            ("time", "start", task.stem, "--by", "agent:test"),
            ("time", "cancel", task.stem, "--by", "agent:test"),
        ):
            result = self.run_mt(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.git(self.root, "rev-parse", "HEAD").stdout.strip(), before, args)
            self.assertEqual(doing.read_bytes(), original, args)

        self.assert_published_mutation("time", "add", task.stem, "5m")
        sessions = self.run_mt("time", "list", task.stem)
        self.assertEqual(sessions.returncode, 0, sessions.stderr)
        session_id = sessions.stdout.split()[0]
        self.assert_published_mutation("time", "remove", session_id)
        self.assertEqual(self.run_mt("time", "start", task.stem, "--by", "agent:test").returncode, 0)
        self.assert_published_mutation("time", "stop", task.stem, "--by", "agent:test")
        self.assertIn("time_entries:", self.git(self.root, "show", f"origin/main:cards/{task.name}").stdout)
        self.assert_published_mutation("move", task.stem, "waiting", "--by", "agent:test")
        self.assert_published_mutation("delete", task.stem)

    def test_auto_sync_failure_preserves_local_mutation(self) -> None:
        self.enable_auto_sync()
        readme = self.root / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nLocal edit.\n", encoding="utf-8")

        result = self.run_mt("add", "Preserve on sync failure")

        self.assertEqual(result.returncode, 1)
        self.assertIn("auto-sync failed", result.stderr)
        self.assertTrue(next(self.root.glob("cards/*preserve-on-sync-failure.md")).is_file())

    def test_sync_generates_message_from_completed_task(self) -> None:
        task = self.add_task()
        first = self.run_mt("sync")
        self.assertEqual(first.returncode, 0, first.stderr)

        completed = self.run_mt("done", task.stem)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = self.run_mt("sync")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Complete task Call dentist", result.stdout)
        message = self.git(self.root, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(message, "Complete task Call dentist")
        remote = self.git(
            self.root,
            "--git-dir",
            str(self.origin),
            "log",
            "-1",
            "--format=%s",
            "refs/heads/main",
        ).stdout.strip()
        self.assertEqual(remote, message)

    def test_sync_accepts_explicit_message(self) -> None:
        self.add_task("Renew passport")
        result = self.run_mt("sync", "-m", "Plan passport renewal")
        self.assertEqual(result.returncode, 0, result.stderr)
        message = self.git(self.root, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(message, "Plan passport renewal")

    def test_sync_shares_custom_status_policy_with_flat_cards(self) -> None:
        policy = self.root / "mt-policy.yaml"
        policy.write_text(policy.read_text().replace("statuses:\n", "statuses:\n  review: Review\n", 1))
        task = self.add_task("Review the draft")
        self.assertEqual(self.run_mt("move", task.stem, "review").returncode, 0)

        result = self.run_mt("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.root, "status", "--porcelain").stdout, "")
        self.assertIn("  review: Review", self.git(self.root, "show", "origin/main:mt-policy.yaml").stdout)
        self.assertIn("status: review", self.git(self.root, "show", f"origin/main:cards/{task.name}").stdout)

        other = Path(self.temporary.name) / "other"
        self.git(self.origin.parent, "clone", "--branch", "main", str(self.origin), str(other))
        listed = subprocess.run([str(other / "mt"), "list", "--status", "review", "--json"],
                                cwd=other, text=True, capture_output=True)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn('"status": "review"', listed.stdout)

    def assert_lifecycle_conflict(self, local_status: str, remote_status: str) -> None:
        task = self.add_task("Shared work")
        body = Path(self.temporary.name) / "action.md"
        body.write_text("Verify the work.\n")
        self.assertEqual(self.run_mt("edit", task.stem, "--set", "next_action=Verify the result.").returncode, 0)
        self.assertEqual(self.run_mt("sync").returncode, 0)
        other = Path(self.temporary.name) / "other"
        self.git(self.origin.parent, "clone", "--branch", "main", str(self.origin), str(other))
        self.git(other, "config", "user.name", "Remote User")
        self.git(other, "config", "user.email", "remote@example.invalid")
        local = self.run_mt("move", task.stem, local_status, "--by", "agent:local")
        self.assertEqual(local.returncode, 0, local.stderr)
        remote = subprocess.run([str(other / "mt"), "move", task.stem, remote_status,
                                 "--by", "agent:remote"], cwd=other, text=True, capture_output=True)
        self.assertEqual(remote.returncode, 0, remote.stderr)
        published = subprocess.run([str(other / "mt"), "sync"], cwd=other, text=True, capture_output=True)
        self.assertEqual(published.returncode, 0, published.stderr)

        result = self.run_mt("sync")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("sync would conflict", result.stderr)
        self.assertFalse((self.root / ".git" / "MERGE_HEAD").exists())
        self.assertIn(f"status: {local_status}", task.read_text())
        self.assertNotIn("<<<<<<<", task.read_text())
        if local_status == "doing":
            frontmatter, _body = tasklib.parse_frontmatter(task.read_text())
            self.assertEqual(tasklib.parse_yaml(frontmatter)["claimed_by"], "agent:local")

    def test_sync_stops_before_conflicting_metadata_status_transitions(self) -> None:
        self.assert_lifecycle_conflict("waiting", "active")

    def test_sync_stops_before_conflicting_claims_in_same_status(self) -> None:
        self.assert_lifecycle_conflict("doing", "doing")

    def test_sync_refuses_origin_that_is_the_app_repository(self) -> None:
        self.git(self.root, "remote", "add", "upstream", str(self.origin))
        task = self.add_task()
        before = self.git(self.root, "rev-parse", "HEAD").stdout

        result = self.run_mt("sync")

        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to sync", result.stderr)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD").stdout, before)
        self.assertEqual(
            self.git(self.root, "ls-tree", "--name-only", "origin/main", "--", f"cards/{task.name}").stdout, ""
        )

    def test_sync_requires_non_task_changes_to_be_clean(self) -> None:
        self.add_task()
        readme = self.root / "README.md"
        original = readme.read_text(encoding="utf-8")
        readme.write_text(original + "\nLocal edit.\n", encoding="utf-8")
        self.git(self.root, "add", "README.md")

        result = self.run_mt("sync")

        self.assertEqual(result.returncode, 1)
        self.assertIn("requires a clean working tree", result.stderr)
        committed = self.git(self.root, "show", "HEAD:README.md").stdout
        self.assertEqual(committed, original)
        staged = self.git(self.root, "diff", "--cached", "--name-only").stdout.splitlines()
        self.assertEqual(staged, ["README.md"])

    def test_sync_with_no_task_changes_pulls_origin_and_pushes_nothing(self) -> None:
        before = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        other = Path(self.temporary.name) / "other"
        self.git(Path(self.temporary.name), "clone", str(self.origin), str(other))
        self.git(other, "checkout", "-B", "main", "origin/main")
        self.git(other, "config", "user.name", "Other User")
        self.git(other, "config", "user.email", "other@example.invalid")
        (other / "cards").mkdir(parents=True, exist_ok=True)
        task = other / "cards" / "2026-01-01-010101-remote-task.md"
        task.write_text(
            """---
id: 2026-01-01-010101-remote-task
status: inbox
priority: medium
type: task
project: null
topic: null
due: null
due_kind: null
created: 2026-01-01
updated: 2026-01-01
tags: []
---

# Remote task

## Content

Remote task

## Next action

Review remote task.

## Notes

""",
            encoding="utf-8",
        )
        self.git(other, "add", ".")
        self.git(other, "commit", "-m", "Add remote task")
        self.git(other, "push", "origin", "HEAD:main")

        result = self.run_mt("sync")
        after = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No local task changes to commit.", result.stdout)
        self.assertIn("Nothing to push.", result.stdout)
        self.assertNotEqual(after, before)
        self.assertTrue((self.root / "cards" / task.name).is_file())

    def test_sync_with_no_origin_changes_is_a_no_op(self) -> None:
        before = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        result = self.run_mt("sync")
        after = self.git(self.root, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No local task changes to commit.", result.stdout)
        self.assertIn("Nothing to push.", result.stdout)
        self.assertEqual(after, before)

    def test_concurrent_sync_commands_are_serialized(self) -> None:
        original_git_text = tasklib.git_text
        first_fetch = threading.Event()
        second_fetch = threading.Event()
        release_fetch = threading.Event()
        fetch_lock = threading.Lock()
        fetch_count = 0
        results: list[int] = []

        def gated_git_text(*arguments: str) -> subprocess.CompletedProcess[str]:
            nonlocal fetch_count
            if arguments == ("fetch", "origin"):
                with fetch_lock:
                    fetch_count += 1
                    number = fetch_count
                if number == 1:
                    first_fetch.set()
                    if not release_fetch.wait(10):
                        raise TimeoutError("test did not release the first sync fetch")
                else:
                    second_fetch.set()
            return original_git_text(*arguments)

        original_validator = tasklib.run_validator
        with (
            patch.object(tasklib, "REPO_ROOT", self.root),
            patch.object(tasklib, "_DEMO_STATE", False),
            patch.object(tasklib, "git_text", gated_git_text),
            patch.object(tasklib, "run_validator", lambda: original_validator(self.root)),
        ):
            first = threading.Thread(
                target=lambda: results.append(tasklib.command_sync(argparse.Namespace(message=None)))
            )
            first.start()
            self.assertTrue(first_fetch.wait(10), "first sync did not reach fetch")

            second_started = threading.Event()

            def run_second_sync() -> None:
                second_started.set()
                results.append(tasklib.command_sync(argparse.Namespace(message=None)))

            second = threading.Thread(target=run_second_sync)
            second.start()
            self.assertTrue(second_started.wait(10))
            self.assertFalse(second_fetch.wait(0.1), "second sync entered Git while first fetch was running")
            release_fetch.set()
            first.join(10)
            second.join(10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(results), [0, 0])
        self.assertEqual(fetch_count, 2)

    def test_task_change_during_fetch_aborts_sync_before_merge(self) -> None:
        original_git_text = tasklib.git_text
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        merge_commands: list[tuple[str, ...]] = []
        results: list[int] = []

        def gated_git_text(*arguments: str) -> subprocess.CompletedProcess[str]:
            if arguments == ("fetch", "origin"):
                fetch_started.set()
                if not release_fetch.wait(10):
                    raise TimeoutError("test did not release sync fetch")
            if arguments and arguments[0] in {"merge", "merge-tree"}:
                merge_commands.append(arguments)
            return original_git_text(*arguments)

        original_validator = tasklib.run_validator
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(tasklib, "REPO_ROOT", self.root),
            patch.object(tasklib, "_DEMO_STATE", False),
            patch.object(tasklib, "git_text", gated_git_text),
            patch.object(tasklib, "run_validator", lambda: original_validator(self.root)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            sync = threading.Thread(
                target=lambda: results.append(tasklib.command_sync(argparse.Namespace(message=None)))
            )
            sync.start()
            self.assertTrue(fetch_started.wait(10), "sync did not reach fetch")
            created = tasklib.command_create(argparse.Namespace(text=["Created during fetch"]))
            self.assertEqual(created, 0)
            release_fetch.set()
            sync.join(10)

        self.assertFalse(sync.is_alive())
        self.assertEqual(results, [1])
        self.assertIn("workspace changed while sync was fetching", stderr.getvalue())
        self.assertEqual(merge_commands, [])
        self.assertEqual(len(list(self.root.glob("cards/*created-during-fetch.md"))), 1)

    def test_sync_cancels_before_conflicting_merge(self) -> None:
        task = self.add_task()
        self.assertEqual(self.run_mt("sync").returncode, 0)
        remote = Path(self.temporary.name) / "remote"
        self.git(Path(self.temporary.name), "clone", str(self.origin), str(remote))
        self.git(remote, "checkout", "-B", "main", "origin/main")
        self.git(remote, "config", "user.name", "Remote User")
        self.git(remote, "config", "user.email", "remote@example.invalid")
        remote_task = remote / task.relative_to(self.root)
        remote_task.write_text(
            remote_task.read_text(encoding="utf-8").replace(
                "Call dentist", "Call orthodontist"
            ),
            encoding="utf-8",
        )
        self.git(remote, "add", ".")
        self.git(remote, "commit", "-m", "Update remote task")
        self.git(remote, "push", "origin", "HEAD:main")
        task.write_text(
            task.read_text(encoding="utf-8").replace("Call dentist", "Call optometrist"),
            encoding="utf-8",
        )

        result = self.run_mt("sync")

        self.assertEqual(result.returncode, 1)
        self.assertIn("sync would conflict with origin/main", result.stderr)
        self.assertEqual(
            self.git(self.root, "diff", "--name-only", "--diff-filter=U").stdout,
            "",
        )
        self.assertNotIn("<<<<<<<", task.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
