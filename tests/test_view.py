from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_fixture import render_dashboard
import support


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "scripts"))
import cli as tasklib
import store
import transaction  # noqa: E402


class MaterializedViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.root = self.home / "workspace"
        ignored = shutil.ignore_patterns(
            ".git", ".mt", "examples", ".DS_Store", "*.swp"
        )
        shutil.copytree(SOURCE_ROOT, self.root, ignore=ignored)
        (self.root / "cards").mkdir(exist_ok=True)
        self.env = os.environ.copy()
        self.env.update({"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config")})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(self, *arguments: str, root: Path | None = None) -> subprocess.CompletedProcess[str]:
        return support.run_cli(root or self.root, *arguments, env=self.env)

    def hide_git(self) -> None:
        binary = self.home / "without-git"
        binary.mkdir(exist_ok=True)
        (binary / "bash").symlink_to("/bin/bash")
        (binary / "python3").symlink_to(sys.executable)
        self.env["PATH"] = str(binary)

    def write_task(
        self,
        filename: str,
        title: str,
        *,
        project: str = "release",
        tags: tuple[str, ...] = ("paper",),
        intent: str = "Migration reference work.",
    ) -> Path:
        tags_yaml = "[]" if not tags else "\n" + "\n".join(f"  - {tag}" for tag in tags)
        path = self.root / "cards" / filename
        properties = {"id": path.stem, "title": title, "kind": "task", "status": "inbox", "priority": "medium", "type": "task", "project": project, "topic": "work", "created": "2026-01-01", "updated": "2026-01-01", "archived": False, "tags": list(tags), "next_action": "Choose the next step."}
        path.write_text(tasklib.card_format.render_document(properties, intent), encoding="utf-8")
        return path

    def write_note(self, filename: str, title: str, *, content: str = "Reference text.") -> Path:
        path = self.root / "cards" / filename
        properties = {"id": path.stem, "title": title, "kind": "note", "priority": "medium", "project": "release", "topic": "work", "created": "2026-01-01", "updated": "2026-01-01", "needs_review": False, "tags": ["paper"]}
        path.write_text(tasklib.card_format.render_document(properties, content), encoding="utf-8")
        return path

    def test_tag_selection_copies_exact_cards_into_fresh_workspace(self) -> None:
        task = self.write_task("paper-task.md", "Paper task")
        note = self.write_note("paper-note.md", "Paper note")
        task_before, note_before = task.read_bytes(), note.read_bytes()

        created = self.run_mt("view", "--tag", "paper")
        view = self.root / ".mt" / "views" / "paper"

        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertTrue(view.is_dir())
        self.assertEqual((view / "cards" / task.name).read_bytes(), task_before)
        self.assertEqual((view / "cards" / note.name).read_bytes(), note_before)
        self.assertEqual(task.read_bytes(), task_before)
        self.assertEqual(note.read_bytes(), note_before)
        notes_view = self.run_mt("view", "--project", "release", "--kind", "note", "--name", "release-notes")
        self.assertEqual(notes_view.returncode, 0, notes_view.stderr)
        notes_copy = self.root / ".mt" / "views" / "release-notes"
        self.assertEqual([path.name for path in (notes_copy / "cards").glob("*.md")], [note.name])
        self.assertFalse((view / "examples").exists())
        self.assertFalse(any((view / "scripts").rglob("__pycache__")))
        marker = json.loads((view / "mt-view.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["name"], "paper")
        self.assertEqual({item["kind"] for item in marker["items"]}, {"task", "note"})
        self.assertTrue(all(len(item["content_hash"]) == 64 for item in marker["items"]))
        validation = self.run_mt("validate", root=view)
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertFalse((view / ".git").exists())

    def test_visibility_selectors_are_the_same_as_list(self) -> None:
        visible = self.write_task("visible.md", "Visible")
        archived = self.write_task("archived.md", "Archived")
        hidden = self.write_task("hidden.md", "Hidden")
        both = self.write_task("both.md", "Both")
        for path, changes in ((archived, ("archived: false", "archived: true")),
                              (hidden, ("topic: work", "topic: work\nhidden: true")),
                              (both, ("archived: false", "archived: true\nhidden: true"))):
            path.write_text(path.read_text(encoding="utf-8").replace(*changes, 1), encoding="utf-8")

        cases = (
            ("default", (), {visible.name}),
            ("archived-only", ("--archived",), {archived.name, both.name}),
            ("hidden-only", ("--hidden",), {hidden.name, both.name}),
            ("both-only", ("--archived", "--hidden"), {both.name}),
            ("include-archived", ("--include-archived",), {visible.name, archived.name}),
            ("include-hidden", ("--include-hidden",), {visible.name, hidden.name}),
            ("include-both", ("--include-archived", "--include-hidden"),
             {visible.name, archived.name, hidden.name, both.name}),
        )
        for name, flags, expected in cases:
            with self.subTest(name=name):
                result = self.run_mt("view", "--project", "release", "--name", name, *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = {path.name for path in (self.root / ".mt" / "views" / name / "cards").glob("*.md")}
                self.assertEqual(actual, expected)

    def test_search_view_preserves_visibility_filters(self) -> None:
        visible = self.write_task("visible-search.md", "Migration visible")
        archived = self.write_task("archived-search.md", "Migration archived")
        hidden = self.write_task("hidden-search.md", "Migration hidden")
        archived.write_text(archived.read_text(encoding="utf-8").replace("archived: false", "archived: true", 1),
                            encoding="utf-8")
        hidden.write_text(hidden.read_text(encoding="utf-8").replace("topic: work", "topic: work\nhidden: true", 1),
                          encoding="utf-8")
        cases = (
            ("search-default", (), {visible.name}),
            ("search-archived", ("--archived",), {archived.name}),
            ("search-hidden", ("--hidden",), {hidden.name}),
            ("search-all", ("--include-archived", "--include-hidden"),
             {visible.name, archived.name, hidden.name}),
        )
        for name, flags, expected in cases:
            with self.subTest(name=name):
                result = self.run_mt("view", "Migration", "--name", name, *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = {path.name for path in (self.root / ".mt" / "views" / name / "cards").glob("*.md")}
                self.assertEqual(actual, expected)

    def test_search_composes_project_filter_and_explicit_name(self) -> None:
        selected = self.write_task("migration-release.md", "Migration launch", intent="migration plan")
        self.write_task("migration-other.md", "Migration elsewhere", project="other", intent="migration")

        created = self.run_mt(
            "view", "migration", "--project", "release", "--name", "launch-review"
        )
        view = self.root / ".mt" / "views" / "launch-review"

        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(sorted(path.name for path in (view / "cards").glob("*.md")), [selected.name])
        self.assertEqual((view / "cards" / selected.name).read_bytes(), selected.read_bytes())

    def test_copy_policy_suppresses_auto_sync_and_identifies_view(self) -> None:
        task = self.write_task("policy-task.md", "Policy task")
        task_before = task.read_bytes()
        shared_policy_before = (self.root / "mt-policy.yaml").read_bytes()
        local_policy = self.root / "mt-policy.local.yaml"
        local_policy.write_text("rules:\n  auto_sync: true\n", encoding="utf-8")
        local_policy_before = local_policy.read_bytes()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "View fixture"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "view@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "Fixture baseline"], cwd=self.root, check=True)
        index_before = (self.root / ".git" / "index").read_bytes()
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, text=True, capture_output=True, check=True
        ).stdout
        source_status_before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root, text=True, capture_output=True, check=True
        ).stdout
        created = self.run_mt("view", "--project", "release")
        view = self.root / ".mt" / "views" / "release"
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertFalse((view / ".git").exists())
        self.assertEqual((view / "mt-policy.yaml").read_bytes(), shared_policy_before)
        self.assertEqual((view / "mt-policy.local.yaml").read_bytes(), local_policy_before)

        policy = self.run_mt("policy", "--json", root=view)
        self.assertEqual(policy.returncode, 0, policy.stderr)
        self.assertFalse(json.loads(policy.stdout)["rules"]["auto_sync"])
        listed = self.run_mt("list", "--json", root=view)
        item = json.loads(listed.stdout)[0]
        edited = self.run_mt("edit", item["ref"], "--set", "priority=high", root=view)
        self.assertEqual(edited.returncode, 0, edited.stderr)
        copied_item = json.loads(self.run_mt("list", "--json", root=view).stdout)[0]
        self.assertEqual(copied_item["priority"], "high")
        self.assertEqual(task.read_bytes(), task_before)
        self.assertEqual((self.root / "mt-policy.yaml").read_bytes(), shared_policy_before)
        self.assertEqual(local_policy.read_bytes(), local_policy_before)
        self.assertEqual((self.root / ".git" / "index").read_bytes(), index_before)
        self.assertEqual(
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, text=True, capture_output=True, check=True).stdout,
            head_before,
        )
        self.assertEqual(
            subprocess.run(["git", "status", "--porcelain"], cwd=self.root, text=True, capture_output=True, check=True).stdout,
            source_status_before,
        )

        info = self.run_mt("info", root=view)
        self.assertIn("View copy: release", info.stdout)
        self.assertIn("".join(f"Source: {self.root}".split()),
                      "".join(info.stdout.replace("│", "").split()))
        self.assertIn("Changes stay in this view copy.", info.stdout)
        page = self.home / "view-dashboard.html"
        dashboard = render_dashboard(view, page)
        self.assertEqual(dashboard.returncode, 0, dashboard.stderr)
        html = page.read_text(encoding="utf-8") + page.with_suffix(".json").read_text(encoding="utf-8")
        self.assertIn('class="mt-view-indicator"', html)
        self.assertIn("View: ' + view.name + ' · copy", html)
        self.assertIn('"name": "release"', html)
        self.assertIn(str(self.root), html)
        self.assertNotIn("command_prefix", html)

    def test_empty_view_and_collision_are_safe(self) -> None:
        created = self.run_mt("view", "--project", "missing")
        view = self.root / ".mt" / "views" / "missing"
        before = (view / "mt-view.json").read_bytes()
        collision = self.run_mt("view", "--project", "missing")

        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertFalse((view / ".git").exists())
        self.assertEqual(list((view / "cards").iterdir()), [])
        self.assertEqual(collision.returncode, 1)
        self.assertIn("already exists", collision.stderr)
        self.assertEqual((view / "mt-view.json").read_bytes(), before)

    def test_view_create_edit_and_delete_work_without_git(self) -> None:
        task = self.write_task("offline.md", "Offline view task")
        original = task.read_bytes()
        self.hide_git()

        created = self.run_mt("view", "--project", "release", "--name", "offline")
        view = self.root / ".mt" / "views" / "offline"

        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertFalse((view / ".git").exists())
        item = json.loads(self.run_mt("list", "--json", root=view).stdout)[0]
        edited = self.run_mt("edit", item["ref"], "--set", "priority=high", root=view)
        self.assertEqual(edited.returncode, 0, edited.stderr)
        self.assertEqual(task.read_bytes(), original)
        deleted = self.run_mt("delete", item["ref"], root=view)
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertFalse((view / "cards" / task.name).exists())
        self.assertEqual(self.run_mt("validate", root=view).returncode, 0)

    def test_invalid_selected_card_fails_candidate_validation_without_publishing(self) -> None:
        task = self.write_task("invalid-structure.md", "Invalid structure")
        raw = task.read_text(encoding="utf-8")
        task.write_text(raw.replace("priority: medium", "priority: invalid"), encoding="utf-8")

        created = self.run_mt("view", "--project", "release")

        self.assertNotEqual(created.returncode, 0)
        self.assertIn("did not validate", created.stderr)
        self.assertFalse((self.root / ".mt" / "views" / "release").exists())
        self.assertEqual(list((self.root / ".mt" / "scratch" / "task-views").iterdir()), [])

    def test_concurrent_same_name_creation_publishes_one_complete_view(self) -> None:
        task = self.write_task("concurrent.md", "Concurrent task")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_mt, "view", "--project", "release") for _ in range(2)]
            results = [future.result() for future in futures]

        self.assertEqual(sorted(result.returncode for result in results), [0, 1])
        view = self.root / ".mt" / "views" / "release"
        self.assertEqual((view / "cards" / task.name).read_bytes(), task.read_bytes())
        self.assertEqual(self.run_mt("validate", root=view).returncode, 0)
        scratch = self.root / ".mt" / "scratch" / "task-views"
        self.assertEqual(list(scratch.iterdir()), [])

    def test_demo_selection_uses_current_discovered_demo_cards(self) -> None:
        fixture = self.write_task("demo-card.md", "Demo selection")
        examples = self.root / "examples" / "cards"
        examples.mkdir(parents=True)
        shutil.move(str(fixture), str(examples / fixture.name))

        created = self.run_mt("view", "--project", "release")
        view = self.root / ".mt" / "views" / "release"
        demo_card = self.root / ".mt" / "demo" / "cards" / "demo-card.md"

        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertTrue(demo_card.is_file())
        marker = json.loads((view / "mt-view.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["items"][0]["path"], ".mt/demo/cards/demo-card.md")
        self.assertEqual((view / "cards" / "demo-card.md").read_bytes(), demo_card.read_bytes())
        self.assertFalse((view / "examples").exists())

    def test_marker_errors_are_visible(self) -> None:
        created = self.run_mt("view", "--project", "release")
        view = self.root / ".mt" / "views" / "release"
        self.assertEqual(created.returncode, 0, created.stderr)

        marker_path = view / "mt-view.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["items"] = [{"kind": [], "path": "cards/a.md", "content_hash": "0" * 64}]
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        invalid = self.run_mt("info", root=view)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("view marker is malformed", invalid.stderr)

        marker["items"] = []
        marker["version"] = True
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        unsupported = self.run_mt("info", root=view)
        self.assertNotEqual(unsupported.returncode, 0)
        self.assertIn("unsupported view marker version", unsupported.stderr)

    def test_selector_hash_and_path_checks_reject_stale_or_escaped_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cards = root / "cards"
            cards.mkdir()
            card = cards / "one.md"
            card.write_bytes(b"original")
            record = {
                "kind": "task",
                "path": "cards/one.md",
                "content_hash": hashlib.sha256(b"original").hexdigest(),
            }
            ws = tasklib.Workspace.at(root)
            with patch.object(store, "cards_root", return_value=cards):
                self.assertEqual(tasklib.validated_view_items([record], ws=ws)[0][2], b"original")
                card.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "changed during view creation"):
                    tasklib.validated_view_items([record], ws=ws)
                escaped = {**record, "path": "../outside.md"}
                with self.assertRaisesRegex(ValueError, "unsafe card path"):
                    tasklib.validated_view_items([escaped], ws=ws)

    def test_repo_selector_runs_view_commands_inside_selected_workspace(self) -> None:
        target = self.home / "target"
        shutil.copytree(
            SOURCE_ROOT,
            target,
            ignore=shutil.ignore_patterns(".git", ".mt", "examples", ".DS_Store"),
        )
        (target / "cards").mkdir(exist_ok=True)
        task = target / "cards" / "selected.md"
        task.write_text(
            """---
id: selected
title: Selected
kind: task
status: inbox
priority: medium
type: task
project: release
topic: work
due: null
due_kind: null
created: 2026-01-01
updated: 2026-01-01
tags: [paper]
---

# Selected

## Content

Content.

## Next action

Take a step.

## Notes
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [str(self.root / "mt"), "-R", str(target), "view", "--project", "release"],
            cwd=self.home,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((target / ".mt" / "views" / "release").is_dir())
        self.assertFalse((self.root / ".mt" / "views").exists())
        self.assertEqual(
            (target / ".mt" / "views" / "release" / "cards" / task.name).read_bytes(), task.read_bytes()
        )

    def test_inapplicable_view_options_fail_instead_of_being_ignored(self) -> None:
        fuzzy = self.run_mt("view", "--project", "release", "--fuzzy")
        scope = self.run_mt("view", "paper", "--kind", "note")
        nameless = self.run_mt("view", "--type", "task")

        self.assertEqual(fuzzy.returncode, 2)
        self.assertIn("require search words", fuzzy.stderr)
        self.assertEqual(scope.returncode, 0, scope.stderr)
        self.assertTrue((self.root / ".mt" / "views" / "paper").is_dir())
        self.assertEqual(nameless.returncode, 2)
        self.assertIn("--name", nameless.stderr)


if __name__ == "__main__":
    unittest.main()

