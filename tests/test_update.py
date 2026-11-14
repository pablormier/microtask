from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.upstream = root / "upstream"
        shutil.copytree(
            SOURCE_ROOT,
            self.upstream,
            ignore=shutil.ignore_patterns(".git", ".mt", ".DS_Store"),
        )
        self.git(self.upstream, "init", "-b", "main")
        self.configure(self.upstream)
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "Initial")

        self.workspace = root / "workspace"
        subprocess.run(
            ["git", "clone", "--no-local", str(self.upstream), str(self.workspace)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.configure(self.workspace)
        self.git(self.workspace, "remote", "rename", "origin", "upstream")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def configure(self, repository: Path) -> None:
        self.git(repository, "config", "user.name", "Update Test")
        self.git(repository, "config", "user.email", "update@example.invalid")

    def run_update(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.workspace / "mt"), "update", *arguments],
            cwd=self.workspace,
            check=False,
            capture_output=True,
            text=True,
        )

    def commit_upstream_change(self, text: str) -> None:
        (self.upstream / "README.md").write_text(text, encoding="utf-8")
        self.git(self.upstream, "add", "README.md")
        self.git(self.upstream, "commit", "-m", "Update application")

    def test_normal_update_merges_upstream(self) -> None:
        self.commit_upstream_change("# Updated upstream\n")

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "README.md").read_text(), "# Updated upstream\n")
        self.assertIn("Application files changed: 1", result.stdout)
        self.assertIn("Modified: README.md", result.stdout)
        self.assertIn("Nothing was pushed", result.stdout)

        repeat = self.run_update()

        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertIn("Already up to date with upstream/main", repeat.stdout)
        self.assertIn("No files changed", repeat.stdout)
        self.assertNotIn("Application files changed: 1", repeat.stdout)

    def test_update_replaces_conflicting_local_application_content(self) -> None:
        (self.workspace / "README.md").write_text("# Workspace version\n", encoding="utf-8")
        self.git(self.workspace, "add", "README.md")
        self.git(self.workspace, "commit", "-m", "Customize workspace")
        self.commit_upstream_change("# Upstream version\n")

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "README.md").read_text(), "# Upstream version\n")
        self.assertEqual(self.git(self.workspace, "status", "--porcelain").stdout, "")

    def test_update_removes_local_only_application_files_and_preserves_tasks(self) -> None:
        added = subprocess.run(
            [str(self.workspace / "mt"), "add", "Workspace task"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        task = Path(added.stdout.strip())
        task_contents = task.read_bytes()
        saved_note = subprocess.run(
            [str(self.workspace / "mt"), "note", "Workspace note"],
            cwd=self.workspace, check=True, capture_output=True, text=True,
        )
        note = Path(saved_note.stdout.strip())
        note_contents = note.read_bytes()
        (self.workspace / "README.md").write_text("# Customized\n", encoding="utf-8")
        (self.workspace / "local-only.txt").write_text("remove me\n", encoding="utf-8")
        self.git(self.workspace, "add", ".")
        self.git(self.workspace, "commit", "-m", "Workspace changes")

        self.commit_upstream_change("# Canonical\n")
        (self.upstream / "upstream-only.txt").write_text("keep me\n", encoding="utf-8")
        self.git(self.upstream, "add", "upstream-only.txt")
        self.git(self.upstream, "commit", "-m", "Add upstream file")

        result = self.run_update("--force")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "README.md").read_text(), "# Canonical\n")
        self.assertFalse((self.workspace / "local-only.txt").exists())
        self.assertTrue((self.workspace / "upstream-only.txt").is_file())
        self.assertIn("Added: upstream-only.txt", result.stdout)
        self.assertIn("Removed: local-only.txt", result.stdout)
        self.assertEqual(task.read_bytes(), task_contents)
        self.assertEqual(note.read_bytes(), note_contents)
        self.assertEqual(self.git(self.workspace, "status", "--porcelain").stdout, "")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "upstream/main", "HEAD"],
            cwd=self.workspace,
            check=False,
        )
        self.assertEqual(ancestor.returncode, 0)
        self.assertIn("Nothing was pushed", result.stdout)

    def test_check_previews_changes_without_updating(self) -> None:
        self.commit_upstream_change("# New upstream\n")
        before = self.git(self.workspace, "rev-parse", "HEAD").stdout.strip()

        result = self.run_update("--check", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        self.assertIn("README.md", "\n".join(preview["application_changes"]))
        self.assertEqual(preview["workspace_data"], "preserved")
        self.assertEqual(self.git(self.workspace, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertNotEqual((self.workspace / "README.md").read_text(), "# New upstream\n")

    def test_update_repairs_broken_parser_without_loading_it(self) -> None:
        tasklib = self.workspace / "scripts" / "cli_parser.py"
        tasklib.write_text(tasklib.read_text().replace(
            "    info.set_defaults(func=command_info)",
            "    info.set_defaults(func=command_triage_snapshot)",
        ), encoding="utf-8")
        self.git(self.workspace, "add", "scripts/cli_parser.py")
        self.git(self.workspace, "commit", "-m", "Broken local parser")
        broken = subprocess.run([str(self.workspace / "mt"), "help"], cwd=self.workspace,
                                text=True, capture_output=True, check=False)
        self.assertNotEqual(broken.returncode, 0)

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tasklib.read_bytes(), (self.upstream / "scripts" / "cli_parser.py").read_bytes())
        self.assertEqual(self.run_update("--check").returncode, 0)
        self.assertEqual(self.git(self.workspace, "status", "--porcelain").stdout, "")

    def test_failed_candidate_rolls_back_application_and_history(self) -> None:
        tasklib = self.upstream / "scripts" / "cli.py"
        tasklib.write_text(tasklib.read_text() + "\nthis is invalid Python\n", encoding="utf-8")
        self.git(self.upstream, "add", "scripts/cli.py")
        self.git(self.upstream, "commit", "-m", "Broken application")
        before = self.git(self.workspace, "rev-parse", "HEAD").stdout.strip()
        original = (self.workspace / "scripts" / "cli.py").read_bytes()

        result = self.run_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate failed", result.stderr)
        self.assertEqual(self.git(self.workspace, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual((self.workspace / "scripts" / "cli.py").read_bytes(), original)
        self.assertEqual(self.git(self.workspace, "status", "--porcelain").stdout, "")

    def test_failed_validation_rolls_back_application_and_history(self) -> None:
        validator = self.upstream / "scripts" / "validation.py"
        validator.write_text(validator.read_text().replace(
            '    """Validate every card and draft in root against policy."""',
            '    """Validate every card and draft in root against policy."""\n    raise SystemExit(1)',
            1,
        ), encoding="utf-8")
        self.git(self.upstream, "add", "scripts/validation.py")
        self.git(self.upstream, "commit", "-m", "Reject candidate")
        before = self.git(self.workspace, "rev-parse", "HEAD").stdout.strip()
        original = (self.workspace / "scripts" / "validation.py").read_bytes()

        result = self.run_update()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate failed validate", result.stderr)
        self.assertEqual(self.git(self.workspace, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual((self.workspace / "scripts" / "validation.py").read_bytes(), original)
        self.assertEqual(self.git(self.workspace, "status", "--porcelain").stdout, "")

    def test_policy_only_upstream_change_finishes_merge_from_other_directory(self) -> None:
        policy = self.upstream / "mt-policy.yaml"
        policy.write_text(policy.read_text() + "\n# Upstream policy note\n", encoding="utf-8")
        self.git(self.upstream, "add", "mt-policy.yaml")
        self.git(self.upstream, "commit", "-m", "Update policy defaults")
        original = (self.workspace / "mt-policy.yaml").read_bytes()

        result = subprocess.run([str(self.workspace / "mt"), "update"], cwd=self.upstream,
                                text=True, capture_output=True, check=False)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "mt-policy.yaml").read_bytes(), original)
        self.assertIn("Update complete: applied 1 upstream commit", result.stdout)
        self.assertIn("No application files changed", result.stdout)
        self.assertEqual(self.git(self.workspace, "status", "--porcelain").stdout, "")
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", "upstream/main", "HEAD"],
                                  cwd=self.workspace, check=False)
        self.assertEqual(ancestor.returncode, 0)

    def test_update_preserves_committed_local_policy_override(self) -> None:
        local_policy = self.workspace / "mt-policy.local.yaml"
        shared_policy = self.workspace / "mt-policy.yaml"
        shared_policy.write_text(shared_policy.read_text() + "\n# Workspace preference\n", encoding="utf-8")
        local_policy.write_text(
            "rules:\n  auto_sync: true\n",
            encoding="utf-8",
        )
        self.git(self.workspace, "add", "mt-policy.yaml", "mt-policy.local.yaml")
        self.git(self.workspace, "commit", "-m", "Add personal policy")
        self.commit_upstream_change("# Updated upstream\n")
        upstream_policy = self.upstream / "mt-policy.yaml"
        upstream_policy.write_text(upstream_policy.read_text() + "\n# Upstream default\n", encoding="utf-8")
        self.git(self.upstream, "add", "mt-policy.yaml")
        self.git(self.upstream, "commit", "-m", "Change default policy")

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            local_policy.read_text(encoding="utf-8"),
            "rules:\n  auto_sync: true\n",
        )
        self.assertIn("# Workspace preference", shared_policy.read_text(encoding="utf-8"))
        self.assertNotIn("# Upstream default", shared_policy.read_text(encoding="utf-8"))
        self.assertEqual(self.git(self.workspace, "status", "--porcelain").stdout, "")
        self.assertIn("Nothing was pushed", result.stdout)

    def test_rewritten_upstream_history_explains_how_to_join_it(self) -> None:
        self.git(self.upstream, "checkout", "--orphan", "rewritten")
        self.git(self.upstream, "commit", "-m", "Rewritten history")
        self.git(self.upstream, "branch", "-M", "rewritten", "main")
        (self.upstream / "README.md").write_text("# After rewrite\n", encoding="utf-8")
        self.git(self.upstream, "commit", "-am", "Change after rewrite")
        before = self.git(self.workspace, "rev-parse", "HEAD").stdout

        for arguments in (("--check",), ()):
            result = self.run_update(*arguments)
            self.assertNotEqual(result.returncode, 0, arguments)
            self.assertIn("share no history", result.stderr)
            self.assertIn("--allow-unrelated-histories", result.stderr)
            self.assertEqual(self.git(self.workspace, "rev-parse", "HEAD").stdout, before)

        self.git(self.workspace, "merge", "-s", "ours", "--allow-unrelated-histories",
                 "-m", "Adopt rewritten microtask history", "upstream/main")
        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workspace / "README.md").read_text(), "# After rewrite\n")

    def test_update_refuses_dirty_workspace(self) -> None:
        (self.workspace / "README.md").write_text("dirty\n", encoding="utf-8")

        result = self.run_update()

        self.assertEqual(result.returncode, 1)
        self.assertIn("requires a clean working tree", result.stderr)

    def test_update_preserves_uncommitted_draft_edits_additions_and_deletions(self) -> None:
        drafts = self.workspace / "drafts"
        def create_draft(title: str, body: str) -> Path:
            created = subprocess.run(
                [str(self.workspace / "mt"), "draft", "create", "note", "--title", title,
                 "--file", "-", "--json"],
                cwd=self.workspace, input=body, check=True, capture_output=True, text=True,
            )
            record = json.loads(created.stdout)
            return drafts / f"{record['id']}.md"

        tracked = create_draft("Tracked draft", "original draft\n")
        deleted = create_draft("Deleted draft", "keep until discarded\n")
        self.git(self.workspace, "add", "drafts")
        self.git(self.workspace, "commit", "-m", "Add drafts")

        tracked_record = json.loads(subprocess.run(
            [str(self.workspace / "mt"), "draft", "read", tracked.stem, "--json"],
            cwd=self.workspace, check=True, capture_output=True, text=True,
        ).stdout)
        subprocess.run(
            [str(self.workspace / "mt"), "draft", "edit", tracked.stem,
             "--expected-revision", str(tracked_record["revision"]), "--file", "-"],
            cwd=self.workspace, input="unsynced edit\n", check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [str(self.workspace / "mt"), "draft", "discard", deleted.stem],
            cwd=self.workspace, check=True, capture_output=True, text=True,
        )
        added = create_draft("New draft", "unsynced new draft\n")
        self.commit_upstream_change("# Updated with local drafts\n")

        result = self.run_update()

        self.assertEqual(result.returncode, 0, result.stderr)
        saved_tracked = json.loads(subprocess.run(
            [str(self.workspace / "mt"), "draft", "read", tracked.stem, "--json"],
            cwd=self.workspace, check=True, capture_output=True, text=True,
        ).stdout)
        self.assertEqual(saved_tracked["body"], "unsynced edit\n")
        self.assertFalse(deleted.exists())
        saved_added = json.loads(subprocess.run(
            [str(self.workspace / "mt"), "draft", "read", added.stem, "--json"],
            cwd=self.workspace, check=True, capture_output=True, text=True,
        ).stdout)
        self.assertEqual(saved_added["body"], "unsynced new draft\n")
        self.assertEqual((self.workspace / "README.md").read_text(encoding="utf-8"), "# Updated with local drafts\n")
        self.assertIn(str(tracked.relative_to(self.workspace)), self.git(self.workspace, "status", "--porcelain").stdout)
        self.assertEqual(self.git(self.workspace, "stash", "list").stdout, "")

    def test_update_preserves_fetch_failure_status_and_diagnostics(self) -> None:
        missing = Path(self.temporary.name) / "missing-upstream"
        self.git(self.workspace, "remote", "set-url", "upstream", str(missing))
        before = self.git(self.workspace, "rev-parse", "HEAD").stdout
        fetched = subprocess.run(
            ["git", "fetch", "upstream"], cwd=self.workspace,
            check=False, capture_output=True, text=True,
        )

        result = self.run_update()

        self.assertNotEqual(fetched.returncode, 0)
        self.assertEqual(result.returncode, fetched.returncode)
        self.assertIn(fetched.stderr.strip(), result.stderr)
        self.assertEqual(self.git(self.workspace, "rev-parse", "HEAD").stdout, before)

if __name__ == "__main__":
    unittest.main()
