from __future__ import annotations

import json
import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "tests"))
from card_files import card_files  # noqa: E402
import tasklib


class DeterministicCLIRegressionTests(unittest.TestCase):
    """Keep core list, search, policy, and note behavior covered."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root,
                        ignore=shutil.ignore_patterns(".git", ".mt", "examples", "__pycache__", ".DS_Store"))
        self.env = os.environ.copy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_task_and_note_capture_share_policy_priority_default(self) -> None:
        for command in ("add", "note"):
            result = self.run_mt(command, "Untriaged " + command)
            self.assertEqual(result.returncode, 0, result.stderr)
        listed = json.loads(self.run_mt("list", "--json").stdout)
        self.assertEqual({item["priority"] for item in listed}, {"none"})
        self.assertEqual(next(item for item in listed if item["title"] == "Untriaged add")["next_action"], "")
        self.assertEqual(self.run_mt("validate").returncode, 0)

        (self.root / "mt-policy.local.yaml").write_text("default_priority: medium\n", encoding="utf-8")
        for command in ("add", "note"):
            result = self.run_mt(command, "Override " + command)
            self.assertEqual(result.returncode, 0, result.stderr)
        listed = json.loads(self.run_mt("list", "--json").stdout)
        priorities = {item["title"]: item["priority"] for item in listed}
        self.assertEqual(priorities["Override add"], "medium")
        self.assertEqual(priorities["Override note"], "medium")
        self.assertEqual(priorities["Untriaged add"], "none")
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_add_ready_tasks_and_claim_doing_atomically(self) -> None:
        for status in ("active", "doing"):
            result = self.run_mt("add", "Read document " + status, "--status", status,
                                 *(("--by", "agent:add") if status == "doing" else ()))
            self.assertEqual(result.returncode, 0, result.stderr)
            task = tasklib.Card.read(Path(result.stdout.strip()))
            self.assertEqual(task.frontmatter["status"], status)
            self.assertEqual(task.next_action, "")
            self.assertFalse(tasklib.next_action_needs_restart_step(task))
            if status == "doing":
                self.assertEqual(task.frontmatter["claimed_by"], "agent:add")
                self.assertTrue(task.frontmatter["claimed_at"])
                self.assertIn("agent:add claimed this task.", task.raw)
            else:
                self.assertNotIn("claimed_by", task.frontmatter)
        result = self.run_mt("add", "Study document", "--status", "active",
                             "--next-action", "Resume at section 3")
        self.assertEqual(result.returncode, 0, result.stderr)
        task = tasklib.Card.read(Path(result.stdout.strip()))
        self.assertEqual(task.next_action, "Resume at section 3")
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_add_rejects_invalid_options_and_rolls_back_at_doing_limit(self) -> None:
        for options in (("--status", "done"), ("--status", "invalid"),
                        ("--by", "agent:add"), ("--status", "doing", "--by", " ")):
            result = self.run_mt("add", "Invalid task", *options)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(card_files(self.root, "task")), [])
        accepted = self.run_mt("add", "Literal lifecycle heading", "--next-action", "## Comments\nInjected")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        accepted_path = Path(accepted.stdout.strip())
        self.assertEqual(len(list(card_files(self.root, "task"))), 1)
        self.assertEqual(self.run_mt("validate").returncode, 0)
        (self.root / "mt-policy.local.yaml").write_text("rules:\n  max_doing_tasks: 1\n")
        self.env["MT_USERNAME"] = "agent:environment"
        first = self.run_mt("add", "First doing", "--status", "doing")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_path = Path(first.stdout.strip())
        first_bytes = first_path.read_bytes()
        self.assertEqual(tasklib.Card.read(first_path).frontmatter["claimed_by"], "agent:environment")
        rejected = self.run_mt("add", "Second doing", "--status", "doing")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("limit exceeded", rejected.stderr)
        self.assertEqual({path.resolve() for path in card_files(self.root, "task")},
                         {accepted_path.resolve(), first_path.resolve()})
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_default_doing_add_claims_without_placeholder_action(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            'default_status: doing\ndefault_username: "agent:policy"\n')
        self.env.pop("MT_USERNAME", None)
        result = self.run_mt("add", "Read the document")
        self.assertEqual(result.returncode, 0, result.stderr)
        task = tasklib.Card.read(Path(result.stdout.strip()))
        self.assertEqual(task.frontmatter["claimed_by"], "agent:policy")
        self.assertEqual(task.next_action, "")

    def test_note_capture_uses_legacy_policy_default_without_unassigned_class(self) -> None:
        policy_path = self.root / "mt-policy.yaml"
        policy_path.write_text(policy_path.read_text().replace(
            '  none: "Unassigned; awaiting triage or reference material"\n', ''
        ).replace("default_priority: none", "default_priority: high"), encoding="utf-8")
        result = self.run_mt("note", "Legacy note")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.run_mt("list", "--json").stdout)[0]["priority"], "high")
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def run_mt(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root, env=self.env,
                              text=True, input=input_text, capture_output=True, check=False)

    def test_scratch_allocates_unique_directories_without_creating_cards(self) -> None:
        plain = self.run_mt("scratch")
        self.assertEqual(plain.returncode, 0, plain.stderr)
        first = Path(plain.stdout.strip())

        structured = self.run_mt("scratch", "--json")
        self.assertEqual(structured.returncode, 0, structured.stderr)
        second = Path(json.loads(structured.stdout)["path"])

        for path in (first, second):
            self.assertTrue(path.is_absolute())
            self.assertEqual(path.parent, (self.root / ".mt" / "scratch").resolve())
            self.assertTrue(path.is_dir())
            self.assertEqual(list(path.iterdir()), [])
        self.assertNotEqual(first, second)
        self.assertEqual(list(card_files(self.root, "task")), [])
        self.assertEqual(list(card_files(self.root, "note")), [])

    def test_invalid_commands_never_capture_or_initialize_workspace_data(self) -> None:
        def snapshot() -> dict[str, bytes]:
            return {path.relative_to(self.root).as_posix(): path.read_bytes()
                    for folder in ("cards", ".mt")
                    for path in (self.root / folder).rglob("*") if path.is_file()}

        invalid = (("import-githux",), ("remember",), ("Plan dentist visit",),
                   ("https://example.org/paper",), ("import-githux", "--json"),
                   ("add",), ("note", "--capture"), ("add", "Example", "--invalid-option"),
                   ("ai",), ("batch", "prepare"), ("artifact", "publish"),
                   ("storage", "allocate-run"), ("sync", "protocol", "prepare"),
                   ("view", "Existing task"),
                   ("edit", "Existing task", "--json"))
        for initialized in (False, True):
            if initialized:
                self.assertEqual(self.run_mt("add", "Existing task").returncode, 0)
                self.assertEqual(self.run_mt("note", "Existing note").returncode, 0)
            before = snapshot()
            for arguments in invalid:
                with self.subTest(initialized=initialized, arguments=arguments):
                    result = self.run_mt(*arguments)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error:", result.stderr)
                    self.assertEqual(snapshot(), before)
            if not initialized:
                self.assertFalse((self.root / ".mt").exists())

    def git(self, *arguments: str) -> str:
        result = subprocess.run(["git", "-C", str(self.root), *arguments], text=True,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def initialize_git(self) -> None:
        self.git("init", "-q")
        self.git("config", "user.name", "Deletion test")
        self.git("config", "user.email", "deletion-test@example.invalid")
        self.git("add", "--", ".gitignore")
        self.git("commit", "-qm", "Initial fixture")

    def hide_git(self) -> None:
        binary = Path(self.temporary.name) / "without-git"
        binary.mkdir(exist_ok=True)
        python = binary / "python3"
        if not python.exists():
            python.symlink_to(sys.executable)
        bash = binary / "bash"
        if not bash.exists():
            bash.symlink_to("/bin/bash")
        self.env["PATH"] = str(binary)

    def test_delete_untracked_task_and_note_by_reference_without_sync_or_staging(self) -> None:
        self.initialize_git()
        for command, text in (("add", "Unsynced task"), ("note", "Unsynced note")):
            result = self.run_mt(command, text)
            self.assertEqual(result.returncode, 0, result.stderr)
        listed = self.run_mt("list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        items = json.loads(listed.stdout)
        self.assertEqual(len(items), 2)
        head = self.git("rev-parse", "HEAD")
        index = (self.root / ".git" / "index").read_bytes()
        for item in items:
            with self.subTest(item=item["title"]):
                result = self.run_mt("delete", item["ref"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("pathspec", result.stderr)
                self.assertFalse((self.root / "cards" / f"{item['id']}.md").exists())
                self.assertFalse((self.root / "cards" / f"{item['id']}.md").exists())
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertEqual((self.root / ".git" / "index").read_bytes(), index)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_delete_tracked_item_only_changes_the_worktree(self) -> None:
        self.initialize_git()
        for changed in (False, True):
            with self.subTest(changed=changed):
                title = f"Tracked item {changed}"
                self.assertEqual(self.run_mt("add", title).returncode, 0)
                path = next(path for path in card_files(self.root, "task")
                            if tasklib.card_format.parse_document(path.read_text())[0]["title"] == title)
                relative = path.relative_to(self.root).as_posix()
                self.git("add", "--", relative)
                self.git("commit", "-qm", "Track fixture item")
                if changed:
                    path.write_text(path.read_text() + "\nLocal changes.\n")
                index = (self.root / ".git" / "index").read_bytes()
                result = self.run_mt("delete", path.stem)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(path.exists())
                self.assertEqual((self.root / ".git" / "index").read_bytes(), index)
                self.assertNotIn(f"D\t{relative}", self.git("diff", "--cached", "--name-status"))
                self.assertIn(f" D {relative}", self.git("status", "--porcelain"))

    def test_local_commands_work_without_git_and_sync_gives_setup_guidance(self) -> None:
        self.hide_git()
        created = self.run_mt("add", "Local only task")
        self.assertEqual(created.returncode, 0, created.stderr)
        note = self.run_mt("note", "Local only note")
        self.assertEqual(note.returncode, 0, note.stderr)
        listed = self.run_mt("list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        items = json.loads(listed.stdout)
        self.assertEqual({item["title"] for item in items}, {"Local only task", "Local only note"})
        task = next(item for item in items if item["title"] == "Local only task")
        self.assertEqual(self.run_mt("read", task["ref"]).returncode, 0)
        self.assertEqual(self.run_mt("search", "Local", "--json").returncode, 0)
        edited = self.run_mt("edit", task["ref"], "--set", "priority=high")
        self.assertEqual(edited.returncode, 0, edited.stderr)
        commented = self.run_mt("comment", task["ref"], "-m", "Updated offline")
        self.assertEqual(commented.returncode, 0, commented.stderr)
        next_action = self.run_mt(
            "edit", task["ref"], "--set", "next_action=Check the local result.",
        )
        self.assertEqual(next_action.returncode, 0, next_action.stderr)
        self.assertEqual(self.run_mt("move", task["ref"], "active").returncode, 0)
        self.assertEqual(self.run_mt("convert", task["ref"], "note").returncode, 0)
        self.assertEqual(self.run_mt("convert", task["ref"], "task").returncode, 0)
        self.assertEqual(self.run_mt("move", task["ref"], "active").returncode, 0)
        self.assertEqual(
            self.run_mt("move", task["ref"], "doing", "--by", "offline-agent").returncode, 0
        )
        started = self.run_mt("time", "start", task["ref"], "--by", "offline-agent")
        self.assertEqual(started.returncode, 0, started.stderr)
        stopped = self.run_mt("time", "stop", task["ref"], "--by", "offline-agent")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(
            self.run_mt("move", task["ref"], "active", "--by", "offline-agent").returncode, 0
        )
        info = self.run_mt("info")
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn("GIT", info.stdout)
        self.assertIn("missing", info.stdout)
        self.assertIn("cards", info.stdout)
        sync = self.run_mt("sync")
        self.assertEqual(sync.returncode, 1)
        self.assertIn("install Git", sync.stderr)
        self.assertIn("git init", sync.stderr)
        self.assertEqual(len(list(card_files(self.root, "task"))), 1)
        for item in items:
            deleted = self.run_mt("delete", item["ref"])
            self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_info_keeps_local_counts_when_git_inspection_fails(self) -> None:
        created = self.run_mt("add", "Still visible with broken Git metadata")
        self.assertEqual(created.returncode, 0, created.stderr)
        (self.root / ".git").write_text("not a Git directory\n", encoding="utf-8")

        info = self.run_mt("info")

        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn("GIT", info.stdout)
        self.assertIn("error", info.stdout)
        self.assertRegex(info.stdout, r"(?m)^  Inbox\s+1\s")
        self.assertIn("1 card", info.stdout)

    def test_info_fits_narrow_terminals_without_color(self) -> None:
        self.env["COLUMNS"] = "32"
        self.env["NO_COLOR"] = "1"

        info = self.run_mt("info")

        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertTrue(all(len(line) <= 32 for line in info.stdout.splitlines()))
        self.assertIn("In progress", info.stdout)
        self.assertNotIn("\033[", info.stdout)

    def test_auto_sync_without_git_keeps_local_success_and_json_valid(self) -> None:
        self.hide_git()
        policy = self.root / "mt-policy.local.yaml"
        policy.write_text("rules:\n  auto_sync: true\n", encoding="utf-8")
        created = self.run_mt("add", "Auto-sync stays local")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("changes remain local", created.stdout)
        note = self.run_mt(
            "note", "--capture", "--file", "-", "--capture-id",
            "12345678-1234-4234-8234-123456789abc", "--created-at",
            "2026-10-14T09:00:00", "--title", "JSON stays valid", "--json",
            input_text="local note content",
        )
        self.assertEqual(note.returncode, 0, note.stderr)
        receipt = json.loads(note.stdout)
        self.assertIn("Git sync is unavailable", receipt["sync_error"])

    def test_auto_sync_without_initialized_repo_keeps_local_success(self) -> None:
        policy = self.root / "mt-policy.local.yaml"
        policy.write_text("rules:\n  auto_sync: true\n", encoding="utf-8")

        created = self.run_mt("add", "Uninitialized Git workspace")

        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertIn("changes remain local", created.stdout)
        self.assertNotIn("Auto-syncing", created.stdout)
        self.assertFalse((self.root / ".git").exists())
        info = self.run_mt("info")
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn("uninitialized", info.stdout)
        self.assertIn("git init", info.stdout)

    def write_task(self, item_id: str, title: str, *, status: str = "active", created: str = "2026-06-20T09:00:00",
                   updated: str = "2026-06-20T09:00:00", due: str = "null", tags: str = "[]",
                   archived: bool = False, priority: str = "medium", completed: str | None = None) -> Path:
        completed_line = f"completed: {completed}\n" if completed else ""
        next_action = "Take the next concrete step." if status not in {"done", "waiting", "inbox"} else "TBD"
        raw = (f"---\nid: {item_id}\ntitle: {json.dumps(title)}\nkind: task\nstatus: {status}\npriority: {priority}\ntype: task\n"
               f"project: null\ntopic: null\ndue: {due}\ndue_kind: {'deadline' if due != 'null' else 'null'}\n"
               f"created: {created}\nupdated: {updated}\n{completed_line}archived: {str(archived).lower()}\ntags: {tags}\n---\n\n"
               f"{title}\n\n## Extra context\n")
        path = self.root / "cards" / f"{item_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        return path

    def test_list_date_ranges_use_calendar_days_and_compose_with_metadata(self) -> None:
        self.write_task("2026-06-20-090000-older", "Older completion", status="done",
                        created="2026-06-20T23:59:00", updated="2026-06-21T23:59:00",
                        completed="2026-06-21T23:59:00", tags="[reading]")
        self.write_task("2026-06-21-090000-boundary", "Boundary completion", status="done",
                        created="2026-06-22T00:01:00", updated="2026-06-22T23:59:00",
                        completed="2026-06-22T00:01:00", tags="[reading]")
        self.write_task("2026-06-22-090000-later", "Later completion", status="done",
                        created="2026-06-23T00:01:00", updated="2026-06-23T00:01:00",
                        completed="2026-06-23T00:01:00")
        listed = self.run_mt("list", "--status", "done", "--updated-since", "2026-06-22",
                             "--updated-before", "2026-06-23", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([item["title"] for item in json.loads(listed.stdout)], ["Boundary completion"])
        created = self.run_mt("list", "--status", "done", "--created-since", "2026-06-22", "--tag", "reading",
                              "--json")
        self.assertEqual([item["title"] for item in json.loads(created.stdout)], ["Boundary completion"])
        completed = self.run_mt("list", "--status", "done", "--completed-before", "2026-06-22", "--json")
        self.assertEqual([item["title"] for item in json.loads(completed.stdout)], ["Older completion"])
        composed = self.run_mt("list", "--status", "done", "--updated-since", "2026-06-22",
                               "--tag", "reading", "--json")
        self.assertEqual([item["title"] for item in json.loads(composed.stdout)], ["Boundary completion"])

    def test_due_filters_compose_and_sort_dates(self) -> None:
        self.write_task("2026-06-20-090000-past", "Past deadline", due="2026-06-20", tags="[deadline]")
        self.write_task("2026-06-21-090000-boundary", "Boundary deadline", due="2026-06-22", tags="[deadline]")
        self.write_task("2026-06-22-090000-future", "Future deadline", due="2026-06-23")
        self.write_task("2026-06-23-090000-undated", "Undated task")
        listed = self.run_mt("list", "--status", "active", "--due-before", "2026-06-22", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual({item["title"] for item in json.loads(listed.stdout)}, {"Past deadline"})
        composed = self.run_mt("list", "--status", "active", "--due-since", "2026-06-22",
                               "--due-before", "2026-06-23", "--tag", "deadline", "--json")
        self.assertEqual([item["title"] for item in json.loads(composed.stdout)], ["Boundary deadline"])

    def test_list_sorts_then_applies_limit_or_tail(self) -> None:
        self.write_task("2026-07-01-090000-old", "Old task", created="2026-07-01T09:00:00")
        self.write_task("2026-07-04-090000-recent", "Recent task", created="2026-07-04T09:00:00")
        self.write_task("2026-07-05-090000-newest", "Newest task", created="2026-07-05T09:00:00")
        note = self.run_mt("note", "Middle note")
        self.assertEqual(note.returncode, 0, note.stderr)
        note_path = card_files(self.root, "note")[0]
        raw = note_path.read_text(encoding="utf-8")
        generated_created = re.search(r"(?m)^created: (.+)$", raw)
        self.assertIsNotNone(generated_created)
        note_path.write_text(raw.replace(generated_created.group(0), "created: 2026-07-03T09:00:00", 1),
                             encoding="utf-8")
        listed = self.run_mt("list", "--sort", "created", "--order", "asc", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([item["title"] for item in json.loads(listed.stdout)],
                         ["Old task", "Middle note", "Recent task", "Newest task"])
        limited = self.run_mt("list", "--sort", "created", "--order", "asc", "--limit", "2", "--json")
        self.assertEqual([item["title"] for item in json.loads(limited.stdout)], ["Old task", "Middle note"])
        tail = self.run_mt("list", "--sort", "created", "--order", "asc", "--tail", "2", "--json")
        self.assertEqual(tail.returncode, 0, tail.stderr)
        self.assertEqual([item["title"] for item in json.loads(tail.stdout)], ["Recent task", "Newest task"])
        tasks = self.run_mt("list", "--kind", "task", "--sort", "created", "--order", "asc", "--tail", "2",
                            "--json")
        self.assertEqual(tasks.returncode, 0, tasks.stderr)
        self.assertEqual([item["title"] for item in json.loads(tasks.stdout)],
                         ["Recent task", "Newest task"])
        notes = self.run_mt("list", "--kind", "note", "--json")
        self.assertEqual(notes.returncode, 0, notes.stderr)
        self.assertEqual([item["title"] for item in json.loads(notes.stdout)], ["Middle note"])
        filtered = self.run_mt("list", "--status", "active", "--tail", "1", "--updated-since", "2026-06-20",
                               "--sort", "created", "--order", "asc", "--json")
        self.assertEqual(filtered.returncode, 0, filtered.stderr)
        self.assertEqual([item["title"] for item in json.loads(filtered.stdout)], ["Newest task"])
        text = self.run_mt("list", "--sort", "created", "--order", "asc", "--tail", "2")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(len(text.stdout.splitlines()), 2)
        self.assertLess(text.stdout.index("Recent task"), text.stdout.index("Newest task"))

    def test_priority_order_and_missing_dates_are_consistent(self) -> None:
        self.write_task("2026-06-20-090000-urgent", "Urgent", priority="urgent", due="2026-06-21")
        self.write_task("2026-06-21-090000-low", "Low", priority="low", due="2026-06-20")
        self.write_task("2026-06-22-090000-none", "None", priority="none")
        priorities = self.run_mt("list", "--kind", "task", "--sort", "priority", "--order", "desc", "--json")
        self.assertEqual([item["title"] for item in json.loads(priorities.stdout)], ["Urgent", "Low", "None"])
        ascending = self.run_mt("list", "--kind", "task", "--sort", "priority", "--order", "asc", "--json")
        self.assertEqual([item["title"] for item in json.loads(ascending.stdout)], ["None", "Low", "Urgent"])
        for order in ("asc", "desc"):
            with self.subTest(order=order):
                dates = self.run_mt("list", "--sort", "due", "--order", order, "--json")
                self.assertEqual(json.loads(dates.stdout)[-1]["title"], "None")

    def test_list_has_no_implicit_caps_and_all_selects_complete_inventory(self) -> None:
        for index in range(15):
            title = f"Enumerate note {index:02d}"
            created = self.run_mt("note", title, "--priority", "high")
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(self.run_mt("validate").returncode, 0)
        hidden_edit = self.run_mt("edit", "Enumerate note 12", "--set", "hidden=true")
        self.assertEqual(hidden_edit.returncode, 0, hidden_edit.stderr)
        self.assertEqual(self.run_mt("validate").returncode, 0)
        archived_edit = self.run_mt("edit", "Enumerate note 13", "--set", "archived=true")
        self.assertEqual(archived_edit.returncode, 0, archived_edit.stderr)
        self.assertEqual(self.run_mt("validate").returncode, 0)
        both_edit = self.run_mt("edit", "Enumerate note 14", "--set", "archived=true")
        self.assertEqual(both_edit.returncode, 0, both_edit.stderr)
        self.assertEqual(self.run_mt("edit", "Enumerate note 14", "--set", "hidden=true").returncode, 0)
        self.assertEqual(self.run_mt("validate").returncode, 0)

        default = self.run_mt("list", "--kind", "note", "--json")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(len(json.loads(default.stdout)), 12)
        all_notes = self.run_mt("list", "--kind", "note", "--all", "--json")
        self.assertEqual(len(json.loads(all_notes.stdout)), 15)
        archived = self.run_mt("list", "--kind", "note", "--archived", "--json")
        self.assertEqual(len(json.loads(archived.stdout)), 2)
        hidden = self.run_mt("list", "--kind", "note", "--hidden", "--json")
        self.assertEqual(len(json.loads(hidden.stdout)), 2)
        intersection = self.run_mt("list", "--kind", "note", "--archived", "--hidden", "--json")
        self.assertEqual(len(json.loads(intersection.stdout)), 1)
        self.assertEqual(len(json.loads(self.run_mt("list", "--kind", "note", "--limit", "4", "--json").stdout)), 4)
        self.assertEqual(len(json.loads(self.run_mt("list", "--kind", "note", "--tail", "3", "--json").stdout)), 3)

        human = self.run_mt("list", "--kind", "note")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertEqual(len(human.stdout.splitlines()), 12)
        self.assertNotIn("Total:", human.stdout)
        self.assertNotIn("notes (", human.stdout)

    def test_full_returns_read_shaped_records_for_list_and_search(self) -> None:
        self.write_task("2026-07-01-090000-old", "Old task", created="2026-07-01T09:00:00")
        self.write_task("2026-07-04-090000-recent", "Recent task", created="2026-07-04T09:00:00")
        self.write_task("2026-07-05-090000-newest", "Newest task", created="2026-07-05T09:00:00")
        for command, extra in (("list", ()), ("search", ("task",))):
            with self.subTest(command=command):
                base = (command, *extra, "--sort", "created", "--order", "asc")
                full = self.run_mt(*base, "--limit", "2", "--full")
                self.assertEqual(full.returncode, 0, full.stderr)
                records = json.loads(full.stdout)
                self.assertEqual([r["properties"]["title"] for r in records], ["Old task", "Recent task"])
                refs = [r["ref"] for r in records]
                read = self.run_mt("read", *refs, "--json")
                self.assertEqual(read.returncode, 0, read.stderr)
                self.assertEqual(records, json.loads(read.stdout))
                self.assertEqual(set(records[0]), {"ref", "path", "content_hash", "properties", "body"})
                self.assertEqual(json.loads(self.run_mt(*base, "--limit", "2", "--json", "--full").stdout), records)
                self.assertEqual(json.loads(self.run_mt(*base, "--tail", "1", "--full").stdout)[0]["properties"]["title"],
                                 "Newest task")
                none = self.run_mt(*base, "--status", "done", "--full")
                self.assertEqual(json.loads(none.stdout), [])
                conflict = self.run_mt(*base, "--full", "--compact")
                self.assertEqual(conflict.returncode, 2)
                self.assertIn("not allowed with argument", conflict.stderr)
        inbox = self.run_mt("add", "Needs triage")
        self.assertEqual(inbox.returncode, 0, inbox.stderr)
        triaged = json.loads(self.run_mt("list", "--triage", "--full").stdout)
        self.assertEqual([r["properties"]["title"] for r in triaged], ["Needs triage"])

    def test_unknown_option_or_command_suggests_the_closest_match(self) -> None:
        for arguments, suggestion in ((("list", "--stauts", "active"), "mt list: did you mean --status"),
                                      (("edit", "@abc", "--tags-add", "x"), "mt edit: did you mean --tag"),
                                      (("lst",), "did you mean list?")):
            with self.subTest(arguments=arguments):
                result = self.run_mt(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(suggestion in result.stderr or "maybe you meant 'list'" in result.stderr,
                                result.stderr)
        result = self.run_mt("list", "--zzzzqq")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --zzzzqq", result.stderr)
        self.assertNotIn("did you mean", result.stderr)

    def test_list_rejects_invalid_slices_and_retired_flags(self) -> None:
        for option in ("--limit", "--tail"):
            for count in ("0", "-1", "abc"):
                with self.subTest(option=option, count=count):
                    result = self.run_mt("list", option, count)
                    self.assertEqual(result.returncode, 2, result.stderr)
        for arguments in (("--limit", "2", "--tail", "2"),):
            with self.subTest(arguments=arguments):
                result = self.run_mt("list", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("not allowed with argument", result.stderr)
        retired = (("--head", "2"), ("--last", "2"), ("--top",),
                   ("--verbose",), ("--include-hidden",), ("--include-archived",), ("active",))
        for arguments in retired:
            with self.subTest(arguments=arguments):
                result = self.run_mt("list", *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_list_selection_filters_and_contradictions(self) -> None:
        self.write_task("2026-06-20-090000-active", "Active bug", status="active", tags="[work]")
        self.write_task("2026-06-21-090000-done", "Done bug", status="done", tags="[work]")
        self.write_task("2026-06-22-090000-other", "Other kind", status="waiting")
        for path in card_files(self.root, "task"):
            if path.stem.endswith(("active", "done")):
                path.write_text(path.read_text(encoding="utf-8").replace("type: task", "type: bug", 1),
                                encoding="utf-8")
        self.assertEqual(self.run_mt("note", "Review this note").returncode, 0)
        note_path = card_files(self.root, "note")[0]
        self.assertEqual(self.run_mt("edit", note_path.stem, "--set", "needs_review=true").returncode, 0)
        self.assertEqual(self.run_mt("validate").returncode, 0)

        statuses = self.run_mt("list", "--status", "active", "--status", "done", "--json")
        self.assertEqual({item["title"] for item in json.loads(statuses.stdout)}, {"Active bug", "Done bug"})
        task_type = self.run_mt("list", "--task-type", "BUG", "--json")
        self.assertEqual({item["title"] for item in json.loads(task_type.stdout)}, {"Active bug", "Done bug"})
        self.assertEqual([item["title"] for item in json.loads(
            self.run_mt("list", "--task-type", "buggy", "--json").stdout)], [])
        review = self.run_mt("list", "--needs-review", "--json")
        self.assertEqual([item["title"] for item in json.loads(review.stdout)], ["Review this note"])
        self.assertEqual(self.run_mt("list", "--project", "missing", "--json").stdout, "[]\n")

        for arguments in (("--kind", "note", "--status", "active"),
                          ("--kind", "note", "--task-type", "bug"),
                          ("--status", "missing")):
            with self.subTest(arguments=arguments):
                result = self.run_mt("list", *arguments, "--json")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)

    def test_list_date_validation_and_json_empty_result(self) -> None:
        for arguments in (("--created-since", "2026-02-30"),
                          ("--updated-since", "2026-06-23", "--updated-before", "2026-06-22"),
                          ("--updated-since", "2026-06-23", "--updated-before", "2026-06-23")):
            with self.subTest(arguments=arguments):
                result = self.run_mt("list", *arguments, "--json")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
        empty = self.run_mt("list", "--project", "missing", "--json")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertEqual(empty.stdout, "[]\n")

    def test_local_policy_defaults_and_nested_overrides_apply_to_add(self) -> None:
        shared_policy = self.root / "mt-policy.yaml"
        shared_policy.write_text(
            shared_policy.read_text(encoding="utf-8").replace(
                '  someday: "Someday" # optional; remove if unused\n', '  someday: "Later" # optional\n'
            ),
            encoding="utf-8",
        )
        (self.root / "mt-policy.local.yaml").write_text(
            'statuses:\n  someday: "Maybe later"\ndefault_status: someday\ndefault_priority: high\ndefault_type: reminder\nrules:\n  max_doing_tasks: 2\n',
            encoding="utf-8")
        created = self.run_mt("add", "Review this later")
        self.assertEqual(created.returncode, 0, created.stderr)
        task = card_files(self.root, "task")[0].read_text(encoding="utf-8")
        self.assertEqual(tasklib.card_format.parse_document(task)[0]['priority'], 'high')
        self.assertIn("type: reminder", task)
        spec = importlib.util.spec_from_file_location("policy_parser_regression", self.root / "scripts" / "policy_parser.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        policy = module.load_policy()
        self.assertEqual(policy["statuses"]["someday"], "Maybe later")
        self.assertEqual(policy["rules"]["max_doing_tasks"], "2")
        self.assertEqual(policy["rules"]["list_done_limit"], "10")

    def test_policy_json_hides_unknown_legacy_settings(self) -> None:
        (self.root / "mt-policy.local.yaml").write_text(
            "ai:\n  model: local-model\nai_workflow_models:\n  update: local-update\n"
            "ai_workflow_reasoning_efforts:\n  update: high\nai_aliases:\n  - old alias\n"
            "rules:\n  auto_create_profiles: false\n  triage_batch_size: 2\n",
            encoding="utf-8")
        result = self.run_mt("policy", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads(result.stdout)
        self.assertNotIn("ai", policy)
        self.assertNotIn("ai_workflow_models", policy)
        self.assertNotIn("ai_workflow_reasoning_efforts", policy)
        self.assertNotIn("ai_aliases", policy)
        self.assertNotIn("auto_create_profiles", policy["rules"])
        self.assertNotIn("triage_batch_size", policy["rules"])

    def test_failed_live_validation_rolls_back_item_transaction(self) -> None:
        path = self.write_task("2026-06-20-090000-rollback", "Original title")
        before = path.read_bytes()
        relative = path.relative_to(self.root).as_posix()
        changed = before.replace(b"title: Original title", b"title: Changed title", 1)
        with patch.object(tasklib, "REPO_ROOT", self.root), \
             patch.object(tasklib, "validate_data_root", side_effect=ValueError("live validation failed")):
            with self.assertRaisesRegex(ValueError, "live validation failed"):
                tasklib.apply_item_transaction({relative: changed}, expected={relative: before}, source_root=self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_managed_temp_directory_is_removed_when_validation_raises(self) -> None:
        script = (
            "import sys\n"
            "sys.path.insert(0, 'scripts')\n"
            "import tasklib\n"
            "try:\n"
            "    with tasklib.managed_temp_directory('failure-') as directory:\n"
            "        open(directory + '/probe', 'w').write('temporary')\n"
            "        raise RuntimeError('validation failed')\n"
            "except RuntimeError:\n"
            "    pass\n"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=self.root,
                                env=self.env, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / ".mt" / "tmp").iterdir()), [])

    def test_archived_items_are_hidden_by_default_and_can_be_selected(self) -> None:
        self.write_task("2026-06-20-090000-visible", "Visible task")
        self.write_task("2026-06-21-090000-archived", "Archived task", archived=True)
        default = self.run_mt("list", "--status", "active", "--json")
        self.assertEqual([item["title"] for item in json.loads(default.stdout)], ["Visible task"])
        archived = self.run_mt("list", "--status", "active", "--archived", "--json")
        self.assertEqual([item["title"] for item in json.loads(archived.stdout)], ["Archived task"])

        self.assertEqual(self.run_mt("edit", "Archived task", "--set", "hidden=true").returncode, 0)
        self.assertEqual(self.run_mt("validate").returncode, 0)
        combined = self.run_mt("list", "--status", "active", "--archived", "--hidden")
        self.assertIn("Archived task (archived, hidden)", combined.stdout)
        self.assertEqual(len(json.loads(self.run_mt("list", "--status", "active", "--hidden", "--json").stdout)), 1)

    def test_hidden_tasks_and_notes_are_filtered_but_remain_addressable(self) -> None:
        visible = self.write_task("2026-06-20-090000-visible", "Visibility real work")
        tracking = self.write_task("2026-06-21-090000-tracking", "Visibility countdown",
                                   due="2026-01-01", priority="urgent")
        original_body = tasklib.parse_frontmatter(tracking.read_text())[1]
        self.assertEqual(self.run_mt("edit", tracking.stem, "--set", "hidden=true").returncode, 0)
        self.assertEqual(tasklib.parse_frontmatter(tracking.read_text())[1], original_body)
        self.assertEqual(self.run_mt("note", "Visibility holiday date").returncode, 0)
        note = card_files(self.root, "note")[0]
        self.assertEqual(self.run_mt("edit", note.stem, "--set", "hidden=true").returncode, 0)

        normal = json.loads(self.run_mt("list", "--json").stdout)
        self.assertEqual([item["title"] for item in normal], ["Visibility real work"])
        self.assertFalse(normal[0]["hidden"])
        included = json.loads(self.run_mt("list", "--all", "--json").stdout)
        self.assertEqual(len(included), 3)
        hidden = json.loads(self.run_mt("list", "--hidden", "--json").stdout)
        self.assertEqual({item["kind"] for item in hidden}, {"task", "note"})
        for item in hidden:
            self.assertTrue(item["hidden"])
            self.assertEqual(self.run_mt("read", item["ref"]).returncode, 0)

        hidden_human = self.run_mt("list", "--hidden")
        self.assertEqual(hidden_human.returncode, 0, hidden_human.stderr)
        self.assertIn("Visibility countdown (hidden)", hidden_human.stdout)
        self.assertIn("Visibility holiday date (hidden)", hidden_human.stdout)
        hidden_search = self.run_mt("search", "Visibility", "--hidden")
        self.assertIn("Visibility holiday date (hidden)", hidden_search.stdout)
        self.assertNotIn("(hidden)", self.run_mt("search", "Visibility real work").stdout)

        search = self.run_mt("search", "Visibility")
        self.assertIn("Visibility real work", search.stdout)
        self.assertNotIn("countdown", search.stdout)
        self.assertNotIn("holiday", search.stdout)
        self.assertIn("countdown", self.run_mt("search", "Visibility", "--all").stdout)
        self.assertNotIn("real work", self.run_mt("search", "Visibility", "--hidden").stdout)
        self.assertIn("Visibility real work", self.run_mt("focus").stdout)
        self.assertNotIn("countdown", self.run_mt("focus", "--next").stdout)

        self.assertEqual(self.run_mt("move", tracking.stem, "waiting").returncode, 0)
        self.assertNotIn("countdown", self.run_mt("followup").stdout)
        self.assertEqual(self.run_mt("move", tracking.stem, "active").returncode, 0)
        self.assertEqual(self.run_mt("move", visible.stem, "waiting").returncode, 0)
        self.assertEqual(self.run_mt("add", "Visibility inbox work").returncode, 0)
        self.assertEqual([item["title"] for item in json.loads(
            self.run_mt("list", "--status", "inbox", "--json").stdout)],
                         ["Visibility inbox work"])

        self.assertEqual(self.run_mt("convert", note.stem, "task").returncode, 0)
        converted = self.root / "cards" / note.name
        self.assertIn("hidden: true", converted.read_text())
        self.assertEqual(self.run_mt("convert", note.stem, "note").returncode, 0)
        self.assertIn("hidden: true", note.read_text())
        self.assertEqual(self.run_mt("edit", note.stem, "--set", "archived=true").returncode, 0)
        self.assertEqual(len(json.loads(self.run_mt("list", "--hidden", "--json").stdout)), 2)
        self.assertEqual(len(json.loads(
            self.run_mt("list", "--hidden", "--archived", "--json").stdout)), 1)

        before = note.read_bytes()
        for value in ("yes", "null", '"true"', "1"):
            invalid = self.run_mt("edit", note.stem, "--set", f"hidden={value}")
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("hidden must be true or false", invalid.stderr)
            self.assertEqual(note.read_bytes(), before)
        self.assertEqual(self.run_mt("edit", tracking.stem, "--set", "hidden=false").returncode, 0)
        self.assertIn("countdown", self.run_mt("search", "Visibility").stdout)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_validation_rejects_non_boolean_hidden_metadata(self) -> None:
        self.assertEqual(self.run_mt("add", "Tracking task").returncode, 0)
        self.assertEqual(self.run_mt("note", "Tracking note").returncode, 0)
        for kind in ("task", "note"):
            path, = card_files(self.root, kind)
            original = path.read_text()
            for value in ('"true"', "null", "1"):
                with self.subTest(kind=kind, value=value):
                    path.write_text(original.replace(f"kind: {kind}", f"kind: {kind}\nhidden: {value}", 1))
                    result = self.run_mt("validate")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("hidden must be true or false", result.stdout + result.stderr)
            path.write_text(original)

    def test_search_match_modes_and_relevance_order(self) -> None:
        note_a = self.run_mt("note", "Dentist appointment")
        note_b = self.run_mt("note", "Dentistry plan")
        self.assertEqual(note_a.returncode, 0, note_a.stderr)
        self.assertEqual(note_b.returncode, 0, note_b.stderr)
        phrase = self.run_mt("search", "dentist appointment")
        self.assertEqual(phrase.returncode, 0, phrase.stderr)
        self.assertIn("Dentist appointment", phrase.stdout)
        self.assertNotIn("Dentistry plan", phrase.stdout)
        all_terms = self.run_mt("search", "dentist", "plan", "--match", "all")
        self.assertEqual(all_terms.returncode, 0, all_terms.stderr)
        self.assertIn("Dentistry plan", all_terms.stdout)
        self.assertNotIn("Dentist appointment", all_terms.stdout)
        any_terms = self.run_mt("search", "appointment", "dentist", "--match", "any")
        self.assertIn("Dentistry plan", any_terms.stdout)
        self.assertIn("Dentist appointment", any_terms.stdout)
        self.assertLess(any_terms.stdout.index("Dentist appointment"), any_terms.stdout.index("Dentistry plan"))
        fuzzy = self.run_mt("search", "dentst", "--match", "fuzzy")
        self.assertEqual(fuzzy.returncode, 0, fuzzy.stderr)
        self.assertIn("fuzzy ", fuzzy.stdout)
        self.assertIn("Dentist appointment", fuzzy.stdout)

    def test_search_ignores_frontmatter_and_does_not_resolve_references(self) -> None:
        created = self.run_mt("note", "Frontmatter matching")
        self.assertEqual(created.returncode, 0, created.stderr)
        note = card_files(self.root, "note")[0]
        raw = note.read_text(encoding="utf-8").replace("tags: []", "tags: [yamlonlyterm]", 1)
        note.write_text(raw, encoding="utf-8")
        self.assertEqual(self.run_mt("validate").returncode, 0)
        no_frontmatter = self.run_mt("search", "yamlonlyterm", "--json")
        self.assertEqual(no_frontmatter.returncode, 0, no_frontmatter.stderr)
        self.assertEqual(no_frontmatter.stdout, "[]\n")
        reference_query = self.run_mt("search", "@not-a-reference", "--json")
        self.assertEqual(reference_query.returncode, 0, reference_query.stderr)
        self.assertEqual(reference_query.stdout, "[]\n")

    def test_search_json_matches_list_records_and_preserves_selection(self) -> None:
        for command, title in (("add", "Dentist appointment"), ("note", "Dentistry plan")):
            created = self.run_mt(command, title)
            self.assertEqual(created.returncode, 0, created.stderr)
        listed = self.run_mt("list", "--all", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        records = {item["title"]: item for item in json.loads(listed.stdout)}
        cases = (
            (("dentist",), ["Dentist appointment", "Dentistry plan"]),
            (("dentist", "plan", "--match", "all"), ["Dentistry plan"]),
            (("dentist", "--kind", "task"), ["Dentist appointment"]),
            (("dentist", "--kind", "note"), ["Dentistry plan"]),
            (("no-such-search-result",), []),
        )
        for arguments, titles in cases:
            with self.subTest(arguments=arguments):
                result = self.run_mt("search", *arguments, "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = json.loads(result.stdout)
                self.assertEqual({item["title"] for item in actual}, set(titles))
                self.assertEqual({item["title"]: item for item in actual},
                                 {title: records[title] for title in titles})

    def test_search_json_errors_are_distinct_from_empty_results(self) -> None:
        for arguments in (("",), ("dentist", "--created-since", "invalid-date"),
                          ("dentist", "--updated-since", "2026-02-01", "--updated-before", "2026-01-01")):
            with self.subTest(arguments=arguments):
                result = self.run_mt("search", *arguments, "--json")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)

    def test_search_sorts_slices_and_rejects_retired_options(self) -> None:
        self.run_mt("note", "Needle title source")
        self.run_mt("note", "Needle appears in body only")
        body_note = next(path for path in card_files(self.root, "note") if "body only" in path.read_text())
        raw = body_note.read_text(encoding="utf-8")
        body_note.write_text(raw.replace("title: Needle appears in body only", "title: Body-only fixture", 1), encoding="utf-8")
        results = self.run_mt("search", "needle", "--sort", "title", "--order", "asc", "--json")
        self.assertEqual(results.returncode, 0, results.stderr)
        records = json.loads(results.stdout)
        self.assertEqual([item["title"] for item in records], ["Body-only fixture", "Needle title source"])
        tail = self.run_mt("search", "needle", "--sort", "title", "--order", "asc", "--tail", "1", "--json")
        self.assertEqual([item["title"] for item in json.loads(tail.stdout)], ["Needle title source"])
        human = self.run_mt("search", "needle", "--sort", "title", "--order", "asc", "--limit", "1")
        self.assertEqual(len(human.stdout.splitlines()), 1)
        for arguments in (("--and", "needle"), ("--or", "needle"), ("--fuzzy", "needle"),
                          ("needle", "--due", "today"),
                          ("needle", "--include-hidden"), ("needle", "--include-archived")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_mt("search", *arguments).returncode, 2)
        type_alias = self.run_mt("search", "needle", "--type", "note", "--json")
        self.assertEqual(type_alias.returncode, 0, type_alias.stderr)
        self.assertEqual(json.loads(type_alias.stdout), [])

    def test_note_creation_preserves_supplied_text_and_notes_listing(self) -> None:
        content = "Remember this\n\nKeep the exact wording."
        created = self.run_mt("note", content)
        self.assertEqual(created.returncode, 0, created.stderr)
        note = card_files(self.root, "note")[0].read_text(encoding="utf-8")
        self.assertEqual(tasklib.card_format.parse_document(note)[1], content)
        listed = self.run_mt("list", "--kind", "note", "--all", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)), 1)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_raw_task_and_note_captures_preserve_multiline_whitespace(self) -> None:
        source = "    first line\n    second line  \n\n"
        task_result = self.run_mt("add", source)
        self.assertEqual(task_result.returncode, 0, task_result.stderr)
        task = card_files(self.root, "task")[0].read_text(encoding="utf-8")
        self.assertEqual(tasklib.card_format.parse_document(task)[0]["title"], "first line second line")
        self.assertEqual(tasklib.card_format.parse_document(task)[1], source)

        note_result = self.run_mt("note", source)
        self.assertEqual(note_result.returncode, 0, note_result.stderr)
        note = card_files(self.root, "note")[0].read_text(encoding="utf-8")
        self.assertEqual(tasklib.card_format.parse_document(note)[0]["title"], "first line second line")
        self.assertEqual(tasklib.card_format.parse_document(note)[1], source)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_generated_capture_titles_preserve_short_text_and_word_boundaries(self) -> None:
        cases = (
            ("  Buy\n milk  ", "Buy milk"),
            ("a" * 80, "a" * 80),
            ("a" * 81, "a" * 79 + "…"),
            ("a" * 75 + " tail", "a" * 75 + " tail"),
            ("a" * 75 + " extra", "a" * 75 + "…"),
            ("a" * 79 + " tail", "a" * 79 + "…"),
            ("αβ " * 30, ("αβ " * 26).rstrip() + "…"),
        )
        for text, title in cases:
            with self.subTest(text=text):
                self.assertEqual(tasklib.capture_title(text), title)

    def test_long_capture_titles_are_bounded_without_changing_body_or_explicit_title(self) -> None:
        source = "  Follow up with Christina about the metabolic analysis and discuss the next experimental steps.\nKeep this detail too.  \n"
        explicit_title = "A deliberately long explicit title " + "detail " * 15
        for arguments, folder, heading in (
            (("add",), "cards", "Content"),
            (("note",), "cards", "Content"),
            (("note", "--capture"), "cards", "Content"),
        ):
            for title in (None, explicit_title):
                with self.subTest(arguments=arguments, title=title):
                    before = set((self.root / folder).glob("*.md"))
                    options = () if title is None else ("--title", title)
                    result = self.run_mt(*arguments, *options, "--file", "-", input_text=source)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    path, = set((self.root / folder).glob("*.md")) - before
                    raw = path.read_text(encoding="utf-8")
                    saved_title = tasklib.card_format.parse_document(raw)[0]["title"]
                    if title is None:
                        self.assertLessEqual(len(saved_title), 80)
                        self.assertTrue(saved_title.endswith("…"))
                        self.assertTrue(" ".join(source.split()).startswith(saved_title[:-1] + " "))
                    else:
                        self.assertEqual(saved_title, title.strip())
                    self.assertEqual(tasklib.card_format.parse_document(raw)[1], source)

    def test_long_file_and_stdin_captures_preserve_content_with_separate_titles(self) -> None:
        source = "### Evidence\n\n" + ("    Detailed result: α and β.  \n\n" * 6000)
        source_path = self.root / "review.md"
        source_path.write_text(source, encoding="utf-8")
        for command, folder, heading in (("add", "cards", "Content"), ("note", "cards", "Content")):
            for use_stdin in (False, True):
                with self.subTest(command=command, stdin=use_stdin):
                    title = f"Concise {command} title {use_stdin}"
                    result = self.run_mt(command, "--title", title, "--file",
                                         "-" if use_stdin else str(source_path),
                                         input_text=source if use_stdin else None)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    cards = [path.read_text(encoding="utf-8") for path in (self.root / folder).glob("*.md")]
                    card = next(raw for raw in cards if tasklib.card_format.parse_document(raw)[0]["title"] == title)
                    self.assertEqual(tasklib.card_format.parse_document(card)[1], source)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_capture_title_also_works_with_inline_text(self) -> None:
        result = self.run_mt("add", "--title", "Migration", "Full migration details\nSecond line.")
        self.assertEqual(result.returncode, 0, result.stderr)
        card = card_files(self.root, "task")[0].read_text(encoding="utf-8")
        self.assertEqual(tasklib.card_format.parse_document(card)[0]["title"], "Migration")
        self.assertEqual(tasklib.card_format.parse_document(card)[1],
            "Full migration details\nSecond line.")

    def test_invalid_capture_sources_fail_before_workspace_initialization(self) -> None:
        invalid_utf8 = self.root / "invalid.txt"
        invalid_utf8.write_bytes(b"\xff")
        for command in ("add", "note"):
            for arguments, source in (
                (("--file", "missing.txt"), None),
                (("--file", str(invalid_utf8)), None),
                (("--file", "-"), " \n\t"),
                (("inline", "--file", "-"), "other"),
                (("--title", " ", "inline"), None),
                (("--title", "Invalid\ntitle", "inline"), None),
            ):
                if command == "note" and arguments == ("--title", "Only a title"):
                    # A bare note (including an optional title) now opens the
                    # writing editor. Noninteractive capture still needs text.
                    arguments = ("--capture", *arguments)
                with self.subTest(command=command, arguments=arguments):
                    result = self.run_mt(command, *arguments, input_text=source)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error:", result.stderr)
                    self.assertFalse((self.root / ".mt").exists())
                    self.assertFalse(list(card_files(self.root, "task")))
                    self.assertFalse(list(card_files(self.root, "note")))


class BulkEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(SOURCE_ROOT, self.root,
                        ignore=shutil.ignore_patterns(".git", ".mt", "examples", "__pycache__", ".DS_Store"))
        self.env = os.environ.copy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.root / "mt"), *args], cwd=self.root, env=self.env,
                              text=True, capture_output=True, check=False)

    def write_task(self, item_id: str, title: str, *, status: str = "active", tags: str = "[]",
                   archived: bool = False) -> Path:
        raw = (
            f"---\nid: {item_id}\ntitle: {json.dumps(title)}\nkind: task\nstatus: {status}\npriority: medium\ntype: task\nproject: null\ntopic: null\n"
            f"due: null\ndue_kind: null\ncreated: 2026-06-20T09:00:00\nupdated: 2026-06-20T09:00:00\n"
            f"archived: {str(archived).lower()}\nhidden: false\ntags: {tags}\n---\n{title}\n"
        )
        path = self.root / "cards" / f"{item_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        return path

    def preview(self, *args: str) -> dict[str, object]:
        result = self.run_mt("bulk-edit", *args, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def save_plan(self, plan: dict[str, object]) -> Path:
        path = self.root / "bulk-plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def test_bulk_preview_uses_status_and_tag_scope_and_apply_changes_only_selection(self) -> None:
        selected = self.write_task("2026-06-20-090000-selected", "Selected batch card", tags="[batch]")
        other_status = self.write_task("2026-06-21-090000-inbox", "Other status batch card", status="inbox", tags="[batch]")
        other_tag = self.write_task("2026-06-22-090000-unmatched", "Other tag card")
        before = {path: path.read_bytes() for path in (selected, other_status, other_tag)}
        plan = self.preview("active", "--tag", "batch", "--set", "priority=high")
        self.assertEqual(plan["version"], 1)
        self.assertEqual((plan["selected"], plan["changed"], plan["unchanged"]), (1, 1, 0))
        self.assertEqual(plan["selection"]["statuses"], ["active"])
        self.assertEqual(len(plan["items"]), 1)
        self.assertEqual(plan["items"][0]["title"], "Selected batch card")
        self.assertIs(plan["items"][0]["changed"], True)
        self.assertEqual(plan["items"][0]["before"]["priority"], "medium")
        self.assertEqual(plan["items"][0]["after"]["priority"], "high")
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        applied = self.run_mt("bulk-edit", "--apply", str(self.save_plan(plan)), "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        receipt = json.loads(applied.stdout)
        self.assertEqual((receipt["selected"], receipt["changed"], receipt["unchanged"]), (1, 1, 0))
        self.assertEqual(tasklib.card_format.parse_document(selected.read_text())[0]['priority'], 'high')
        self.assertEqual(other_status.read_bytes(), before[other_status])
        self.assertEqual(other_tag.read_bytes(), before[other_tag])
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_bulk_selection_excludes_hidden_and_archived_until_requested(self) -> None:
        self.write_task("2026-06-20-090000-visible", "Visible bulk card")
        hidden = self.write_task("2026-06-21-090000-hidden", "Hidden bulk card")
        self.write_task("2026-06-22-090000-archived", "Archived bulk card", archived=True)
        hidden_result = self.run_mt("edit", hidden.stem, "--set", "hidden=true")
        self.assertEqual(hidden_result.returncode, 0, hidden_result.stderr)
        self.assertEqual(self.run_mt("validate").returncode, 0)

        default = self.preview("active", "--set", "priority=high")
        self.assertEqual([item["title"] for item in default["items"]], ["Visible bulk card"])
        including_hidden = self.preview("active", "--include-hidden", "--set", "priority=high")
        self.assertEqual({item["title"] for item in including_hidden["items"]},
                         {"Visible bulk card", "Hidden bulk card"})
        including_both = self.preview("active", "--include-hidden", "--include-archived", "--set", "priority=high")
        self.assertEqual({item["title"] for item in including_both["items"]},
                         {"Visible bulk card", "Hidden bulk card", "Archived bulk card"})
        hidden_only = self.preview("active", "--hidden", "--set", "priority=high")
        self.assertEqual([item["title"] for item in hidden_only["items"]], ["Hidden bulk card"])
        archived_only = self.preview("active", "--archived", "--set", "priority=high")
        self.assertEqual([item["title"] for item in archived_only["items"]], ["Archived bulk card"])
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_bulk_noop_is_reported_and_preserves_hash_and_updated(self) -> None:
        task = self.write_task("2026-06-20-090000-noop", "Already high")
        task.write_text(task.read_text(encoding="utf-8").replace("priority: medium", "priority: high", 1),
                        encoding="utf-8")
        before = task.read_bytes()
        plan = self.preview("active", "--set", "priority=high")
        self.assertEqual(len(plan["items"]), 1)
        self.assertIs(plan["items"][0]["changed"], False)
        self.assertEqual(plan["items"][0]["before"], plan["items"][0]["after"])
        result = self.run_mt("bulk-edit", "--apply", str(self.save_plan(plan)), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual((receipt["changed"], receipt["unchanged"]), (0, 1))
        self.assertEqual(task.read_bytes(), before)

    def test_bulk_edit_many_notes_including_hidden_and_archived_to_unassigned_then_reports_noop(self) -> None:
        titles = [f"Bulk null note {index:02d}" for index in range(12)]
        for title in titles:
            created = self.run_mt("note", title, "--priority", "high")
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(self.run_mt("validate").returncode, 0)
        for title, field in ((titles[10], "hidden"), (titles[11], "archived")):
            edited = self.run_mt("edit", title, "--set", f"{field}=true")
            self.assertEqual(edited.returncode, 0, edited.stderr)
            self.assertEqual(self.run_mt("validate").returncode, 0)

        plan = self.preview("--kind", "note", "--include-hidden", "--include-archived", "--set", "priority=none")
        self.assertEqual((plan["selected"], plan["changed"], plan["unchanged"]), (12, 12, 0))
        applied = self.run_mt("bulk-edit", "--apply", str(self.save_plan(plan)), "--json")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["changed"], 12)
        self.assertEqual(self.run_mt("validate").returncode, 0)

        no_op = self.preview("--kind", "note", "--include-hidden", "--include-archived", "--set", "priority=none")
        self.assertEqual((no_op["selected"], no_op["changed"], no_op["unchanged"]), (12, 0, 12))
        receipt = self.run_mt("bulk-edit", "--apply", str(self.save_plan(no_op)), "--json")
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        self.assertEqual((json.loads(receipt.stdout)["changed"], json.loads(receipt.stdout)["unchanged"]), (0, 12))
        listed = self.run_mt("list", "--kind", "note", "--all", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(len(json.loads(listed.stdout)), 12)
        self.assertTrue(all(item["priority"] == "none" for item in json.loads(listed.stdout)))
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_bulk_preview_rejects_empty_selection(self) -> None:
        result = self.run_mt("bulk-edit", "active", "--tag", "no-such-bulk-tag", "--set", "priority=high", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no cards match", result.stderr)

    def test_bulk_apply_rejects_membership_change_after_preview(self) -> None:
        first = self.write_task("2026-06-20-090000-membership-first", "First membership card")
        plan = self.preview("active", "--set", "priority=high")
        plan_path = self.save_plan(plan)
        second = self.write_task("2026-06-21-090000-membership-second", "New membership card")
        self.assertEqual(self.run_mt("validate").returncode, 0)
        before = {first: first.read_bytes(), second: second.read_bytes()}

        result = self.run_mt("bulk-edit", "--apply", str(plan_path))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("membership changed", result.stderr)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_bulk_apply_stale_second_card_leaves_first_unchanged(self) -> None:
        first = self.write_task("2026-06-20-090000-stale-first", "First stale card")
        second = self.write_task("2026-06-21-090000-stale-second", "Second stale card")
        plan = self.preview("active", "--set", "priority=high")
        self.assertEqual(plan["selected"], 2)
        plan_path = self.save_plan(plan)
        changed = self.run_mt("edit", second.stem, "--set", "priority=low")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(self.run_mt("validate").returncode, 0)
        first_before_apply = first.read_bytes()
        second_before_apply = second.read_bytes()

        result = self.run_mt("bulk-edit", "--apply", str(plan_path))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("conflict", result.stderr)
        self.assertEqual(first.read_bytes(), first_before_apply)
        self.assertEqual(second.read_bytes(), second_before_apply)

    def test_bulk_plan_summary_counts_are_validated_before_apply(self) -> None:
        task = self.write_task("2026-06-20-090000-counts", "Counts card")
        plan = self.preview("active", "--set", "priority=high")
        plan["changed"] = 0
        result = self.run_mt("bulk-edit", "--apply", str(self.save_plan(plan)))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("counts do not match", result.stderr)
        self.assertIn("priority: medium", task.read_text(encoding="utf-8"))
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_bulk_apply_rejects_stale_card_hash_without_partial_changes(self) -> None:
        task = self.write_task("2026-06-20-090000-stale", "Stale batch card")
        plan = self.preview("active", "--set", "priority=high")
        plan_path = self.save_plan(plan)
        changed = self.run_mt("edit", task.stem, "--set", "priority=low")
        self.assertEqual(changed.returncode, 0, changed.stderr)
        before_apply = task.read_bytes()
        stale = self.run_mt("bulk-edit", "--apply", str(plan_path))
        self.assertEqual(stale.returncode, 1, stale.stderr)
        self.assertIn("conflict", stale.stderr)
        self.assertEqual(task.read_bytes(), before_apply)
        self.assertEqual(self.run_mt("validate").returncode, 0)

    def test_bulk_transaction_rolls_back_all_cards_when_post_write_validation_fails(self) -> None:
        first = self.write_task("2026-06-20-090000-first", "First rollback card")
        second = self.write_task("2026-06-21-090000-second", "Second rollback card")
        plan = self.preview("active", "--set", "priority=high")
        self.assertEqual(len(plan["items"]), 2)
        plan_path = self.save_plan(plan)
        before = {first: first.read_bytes(), second: second.read_bytes()}
        arguments = argparse.Namespace(apply_plan=str(plan_path), as_json=False)
        with patch.object(tasklib, "REPO_ROOT", self.root), patch.object(tasklib, "_DEMO_STATE", False), \
             patch.object(tasklib, "validate_data_root", side_effect=ValueError("forced validation failure")):
            result = tasklib._command_bulk_edit_apply(arguments)
        self.assertEqual(result, 1)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.run_mt("validate").returncode, 0)


if __name__ == "__main__":
    unittest.main()
