"""mt migrate: move an old-style workspace (app code and cards together) into workspace/."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import legacy_workspace, make_release


SOURCE_ROOT = Path(__file__).resolve().parent.parent
MESSAGE = "Move microtask app out of the workspace"


class MigrateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "gitconfig").write_text("[user]\n\tname = Migrate Test\n\temail = migrate@example.invalid\n")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("MT_", "GIT_"))}
        self.env.update({"HOME": str(self.root), "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
                         "GIT_CONFIG_NOSYSTEM": "1", "MT_USERNAME": "tester"})
        archive = make_release(self.root / "release.tar.gz", "v0.1.0")
        shutil.unpack_archive(archive, self.root / "unpacked")
        self.app = self.root / "microtask"
        shutil.move(str(next((self.root / "unpacked").iterdir())), self.app)
        legacy_workspace(self.app)
        (self.app / "drafts").mkdir(exist_ok=True)
        self.mt("add", "Old card")
        (self.app / "settings.local.yaml").write_text("rules:\n  timer_warn_hours: 4\n")
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "Everything together")
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "init", "--quiet", "--bare", str(self.origin)], env=self.env, check=True)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "--quiet", "-u", "origin", "main")
        self.app_files = [name for name in self.git("ls-files").splitlines()
                          if not name.startswith(("cards/", "drafts/", ".mt/"))
                          and name not in {"settings.yaml", "settings.local.yaml", ".gitignore"}]

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *arguments], cwd=cwd or self.app, env=self.env, text=True,
                              capture_output=True, check=True).stdout.strip()

    def mt(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.app / "mt"), *arguments], cwd=self.root, env=env or self.env, text=True,
                              capture_output=True, check=False, stdin=subprocess.DEVNULL)

    def listing(self) -> list[str]:
        return sorted(str(path.relative_to(self.app)) for path in self.app.rglob("*")
                      if ".git" not in path.relative_to(self.app).parts or path.name == ".git")

    def test_moves_data_and_history_and_commits_the_removal_of_the_app_files(self) -> None:
        workspace = self.app / "workspace"
        commits_before = int(self.git("rev-list", "--count", "HEAD"))
        remote_before = self.git("rev-parse", "main", cwd=self.origin)
        (self.app / "drafts" / "wip.md").write_text("an uncommitted draft\n")

        result = self.mt("migrate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Your workspace is now {workspace}", result.stdout)
        self.assertIn("Nothing was pushed", result.stdout)
        self.assertIn("other machines still on the old layout will lose their app files", result.stdout)
        self.assertIn("run the installer or 'mt migrate' before syncing", result.stdout)
        for name in (".git", "cards", "drafts", ".mt", "settings.yaml", "settings.local.yaml"):
            self.assertFalse((self.app / name).exists(), name)
            if name != ".mt":
                self.assertTrue((workspace / name).exists(), name)
        self.assertTrue((workspace / "drafts" / "wip.md").is_file())
        self.assertTrue((self.app / "mt").is_file())
        self.assertTrue((self.app / "scripts" / "cli.py").is_file())

        self.assertEqual(int(self.git("rev-list", "--count", "HEAD", cwd=workspace)), commits_before + 1)
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=workspace), MESSAGE)
        changes = dict(line.split("\t")[::-1] for line in
                       self.git("show", "--name-status", "--format=", "HEAD", cwd=workspace).splitlines())
        deleted = sorted(name for name, status in changes.items() if status == "D")
        # settings.local.yaml was tracked; it stops syncing but stays on disk. The app's
        # AGENTS.md and CLAUDE.md become the workspace's instructions for agents.
        agent_files = ["AGENTS.md", "CLAUDE.md"]
        self.assertEqual(deleted, sorted([name for name in self.app_files if name not in agent_files]
                                         + ["settings.local.yaml"]))
        for name in agent_files:
            self.assertNotEqual(changes.get(name), "D")
            self.assertEqual(self.git("ls-files", name, cwd=workspace), name)
            self.assertEqual((workspace / name).read_bytes(),
                             (self.app / "templates" / f"workspace-{name}").read_bytes())
        self.assertEqual((workspace / "settings.local.yaml").read_text(), "rules:\n  timer_warn_hours: 4\n")
        self.assertEqual(self.git("ls-files", "settings.local.yaml", cwd=workspace), "")
        self.assertFalse(any(name.startswith(("cards/", "drafts/")) for name in changes))
        ignore = (workspace / ".gitignore").read_text().splitlines()
        self.assertIn(".mt/", ignore)
        self.assertIn("settings.local.yaml", ignore)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all", cwd=workspace), "?? drafts/wip.md")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.origin), remote_before, "something was pushed")

        listed = self.mt("list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("Old card", listed.stdout)
        self.assertIn(str(workspace), listed.stderr)
        self.assertIn("migrate", self.mt("migrate").stderr)

    def test_attachments_move_with_the_cards(self) -> None:
        workspace = self.app / "workspace"
        (self.app / "attachments").mkdir()
        (self.app / "attachments" / "diagram.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n")
        self.git("add", "-f", "attachments/diagram.svg")
        self.git("commit", "--quiet", "-m", "Add an attachment")
        app_files = [name for name in self.app_files]

        described = self.mt("migrate", "--check")

        self.assertEqual(described.returncode, 0, described.stderr)
        self.assertRegex(described.stdout, r"Move into .*workspace: .*\battachments\b")
        self.assertIn(f"delete {len(app_files)} application file(s)", described.stdout)
        result = self.mt("migrate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.app / "attachments").exists())
        self.assertTrue((workspace / "attachments" / "diagram.svg").is_file())
        self.assertEqual(self.git("ls-files", "attachments", cwd=workspace), "attachments/diagram.svg")
        changes = dict(line.split("\t")[::-1] for line in
                       self.git("show", "--name-status", "--format=", "HEAD", cwd=workspace).splitlines())
        self.assertNotIn("attachments/diagram.svg", changes)

    def test_check_describes_the_move_and_changes_nothing(self) -> None:
        before = self.listing()
        head = self.git("rev-parse", "HEAD")

        result = self.mt("migrate", "--check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("workspace", result.stdout)
        self.assertIn(MESSAGE, result.stdout)
        self.assertIn(f"delete {len(self.app_files)} application file(s)", result.stdout)
        self.assertIn("Nothing was changed", result.stdout)
        self.assertEqual(self.listing(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse((self.app / "workspace").exists())

    def test_check_and_run_tell_the_user_to_stop_the_dashboard(self) -> None:
        self.assertIn("stop 'mt web'", self.mt("migrate", "--check").stdout)
        self.assertIn("stop 'mt web'", self.mt("migrate").stdout)

    def test_removes_an_upstream_remote_that_points_at_the_application(self) -> None:
        self.git("remote", "add", "upstream", "https://github.com/pablormier/microtask.git")
        checked = self.mt("migrate", "--check")
        self.assertIn("'upstream' remote", checked.stdout)
        self.assertIn("upstream", self.git("remote").split())

        result = self.mt("migrate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Removed the 'upstream' remote", result.stdout)
        self.assertEqual(self.git("remote", cwd=self.app / "workspace").split(), ["origin"])

    def test_keeps_an_upstream_remote_that_points_elsewhere(self) -> None:
        self.git("remote", "add", "upstream", str(self.origin))
        self.assertEqual(self.mt("migrate").returncode, 0)
        self.assertEqual(self.git("remote", cwd=self.app / "workspace").split(), ["origin", "upstream"])

    def test_a_failed_move_restores_the_upstream_remote(self) -> None:
        url = "https://github.com/pablormier/microtask.git"
        self.git("remote", "add", "upstream", url)
        hook = self.app / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        result = self.mt("migrate")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.app / "workspace").exists())
        self.assertEqual(self.git("remote", "get-url", "upstream"), url)

    def test_refuses_when_unsafe_and_changes_nothing(self) -> None:
        def refused(reason: str) -> None:
            before = self.listing()
            result = self.mt("migrate")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(reason, result.stderr)
            self.assertEqual(self.listing(), before)

        with self.subTest("uncommitted application change"):
            (self.app / "README.md").write_text("edited\n")
            refused("uncommitted changes")
            self.git("checkout", "--", "README.md")
        with self.subTest("untracked file outside drafts"):
            (self.app / "stray.txt").write_text("x\n")
            refused("stray.txt")
            (self.app / "stray.txt").unlink()
        with self.subTest("staged draft"):
            (self.app / "drafts" / "staged.md").write_text("x\n")
            self.git("add", "-f", "drafts/staged.md")
            refused("drafts/staged.md")
            self.git("reset", "--quiet")
            (self.app / "drafts" / "staged.md").unlink()
        with self.subTest("origin is the application repository"):
            self.git("remote", "set-url", "origin", "git@github.com:pablormier/microtask.git")
            refused("application repository")
            self.git("remote", "set-url", "origin", str(self.origin))
        with self.subTest("workspace folder exists"):
            (self.app / "workspace").mkdir()
            refused("already exists")
            (self.app / "workspace").rmdir()
        with self.subTest("not an old-style workspace"):
            shutil.rmtree(self.app / "cards")
            refused("nothing to migrate")

    def test_a_folder_without_git_moves_only_the_data(self) -> None:
        shutil.rmtree(self.app / ".git")

        result = self.mt("migrate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / "workspace" / "cards").is_dir())
        self.assertFalse((self.app / "workspace" / ".git").exists())
        self.assertNotIn("Warning", result.stdout)

    def test_a_workspace_elsewhere_is_refused(self) -> None:
        result = self.mt("migrate", env={**self.env, "MT_WORKSPACE": str(self.root / "elsewhere")})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MT_WORKSPACE", result.stderr)


if __name__ == "__main__":
    unittest.main()
