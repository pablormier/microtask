"""mt-policy.yaml became settings.yaml: the settings command, detection of the old name, and mt migrate's rename."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import ROOT, legacy_workspace, make_release

sys.path.insert(0, str(ROOT / "scripts"))
import workspace as workspace_module  # noqa: E402

RENAME_MESSAGE = "Rename mt-policy.yaml to settings.yaml"
MOVE_MESSAGE = "Move microtask app out of the workspace"
LOCAL = "rules:\n  max_doing_tasks: 4\n"


class SettingsRenameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "gitconfig").write_text("[user]\n\tname = Rename Test\n\temail = rename@example.invalid\n")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("MT_", "GIT_"))}
        self.env.update({"HOME": str(self.root), "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
                         "GIT_CONFIG_NOSYSTEM": "1", "MT_USERNAME": "tester"})
        archive = make_release(self.root / "release.tar.gz", "v0.2.0")
        shutil.unpack_archive(archive, self.root / "unpacked")
        self.app = self.root / "app"
        shutil.move(str(next((self.root / "unpacked").iterdir())), self.app)
        self.default = self.app / "workspace"

    def git(self, folder: Path, *arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=folder, env=self.env, text=True,
                              capture_output=True, check=True).stdout.strip()

    def mt(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(self.app / "mt"), *arguments], cwd=self.root, env=env or self.env, text=True,
                              capture_output=True, check=False, stdin=subprocess.DEVNULL)

    def old_name_folder(self, folder: Path, *, git: bool, local: bool = True) -> Path:
        """A workspace folder from before 0.2.0: cards and mt-policy.yaml, no app."""
        workspace_module.create_workspace(folder, self.app)
        added = self.mt("add", "Kept card", env={**self.env, "MT_WORKSPACE": str(folder)})
        self.assertEqual(added.returncode, 0, added.stderr)
        shutil.rmtree(folder / ".mt", ignore_errors=True)
        (folder / "settings.yaml").rename(folder / "mt-policy.yaml")
        (folder / ".gitignore").write_text(".mt/\nmt-policy.local.yaml\n")
        if local:
            (folder / "mt-policy.local.yaml").write_text(LOCAL)
        if git:
            self.git(folder, "init", "--quiet", "--initial-branch=main")
            self.git(folder, "add", "-A", "-f")
            self.git(folder, "commit", "--quiet", "-m", "Workspace before 0.2.0")
        return folder

    def old_style_install(self) -> None:
        """Cards and mt-policy.yaml next to the app code, in Git, with the local file committed."""
        legacy_workspace(self.app)
        (self.app / "settings.yaml").rename(self.app / "mt-policy.yaml")
        (self.app / "mt-policy.local.yaml").write_text(LOCAL)
        self.git(self.app, "init", "--quiet", "--initial-branch=main")
        self.git(self.app, "add", "-A", "-f")
        self.git(self.app, "commit", "--quiet", "-m", "Everything together")

    def listing(self, folder: Path) -> list[str]:
        return sorted(str(path.relative_to(folder)) for path in folder.rglob("*")
                      if ".git" not in path.relative_to(folder).parts)

    # The command

    def test_mt_settings_shows_the_effective_settings_and_mt_policy_is_gone(self) -> None:
        workspace_module.create_workspace(self.default, self.app)

        shown = self.mt("settings", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("statuses", json.loads(shown.stdout))
        self.assertIn("Effective workspace settings", self.mt("settings").stdout)
        self.assertIn("settings", self.mt("--help").stdout)

        gone = self.mt("policy", "--json")
        self.assertEqual(gone.returncode, 2)
        self.assertIn("invalid choice", gone.stderr)

    # An old-style install: cards next to the app, with the old file name

    def test_an_old_style_install_with_the_old_name_is_still_detected(self) -> None:
        self.old_style_install()

        self.assertTrue(workspace_module.is_legacy_workspace(self.app))
        self.assertFalse(workspace_module.is_data_workspace(self.app))
        # It keeps working on its old file names until 'mt migrate'. The old Git-based
        # 'mt update' runs 'mt help' and 'mt validate' on the new code before accepting it.
        for arguments in (("help",), ("validate",), ("list",), ("add", "Still works")):
            with self.subTest(arguments=arguments):
                result = self.mt(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mt migrate", self.mt("validate").stderr)
        settings = json.loads(self.mt("settings", "--json").stdout)
        self.assertEqual(str(settings["rules"]["max_doing_tasks"]), "4")
        self.assertTrue(any("still-works" in path.name for path in (self.app / "cards").glob("*.md")))
        self.assertFalse(self.default.exists())
        self.assertFalse((self.app / "settings.yaml").exists())
        init = self.mt("init")
        self.assertEqual(init.returncode, 1)
        self.assertIn("mt migrate", init.stderr)
        self.assertFalse(self.default.exists())

    def test_update_refuses_an_old_style_install_with_the_old_name(self) -> None:
        legacy_workspace(self.app)
        (self.app / "settings.yaml").rename(self.app / "mt-policy.yaml")

        update = self.mt("update", "--check", env={**self.env, "MT_RELEASE_TAG": "v9.9.9",
                                                    "MT_ARCHIVE_URL": (self.root / "none.tar.gz").as_uri()})

        self.assertNotEqual(update.returncode, 0)
        self.assertIn("mt migrate", update.stderr)

    def test_migrate_renames_both_files_in_the_one_commit_of_the_move(self) -> None:
        self.old_style_install()
        before = self.listing(self.app)

        checked = self.mt("migrate", "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("Rename mt-policy.yaml to settings.yaml", checked.stdout)
        self.assertIn("Rename mt-policy.local.yaml to settings.local.yaml", checked.stdout)
        self.assertEqual(self.listing(self.app), before)

        commits = int(self.git(self.app, "rev-list", "--count", "HEAD"))
        result = self.mt("migrate")
        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.default
        for name in ("mt-policy.yaml", "mt-policy.local.yaml"):
            self.assertFalse((workspace / name).exists(), name)
            self.assertFalse((self.app / name).exists(), name)
        self.assertTrue((workspace / "settings.yaml").is_file())
        self.assertEqual((workspace / "settings.local.yaml").read_text(), LOCAL)
        self.assertEqual(int(self.git(workspace, "rev-list", "--count", "HEAD")), commits + 1)
        self.assertEqual(self.git(workspace, "log", "-1", "--format=%s"), MOVE_MESSAGE)
        status = self.git(workspace, "diff", "-M", "--name-status", "HEAD~1", "HEAD",
                          "--", "mt-policy.yaml", "settings.yaml").splitlines()
        self.assertEqual(status, ["R100\tmt-policy.yaml\tsettings.yaml"])
        self.assertEqual(self.git(workspace, "ls-files", "mt-policy.local.yaml", "settings.local.yaml"), "")
        self.assertIn("settings.yaml", self.git(workspace, "ls-files"))
        self.assertIn("settings.local.yaml", (workspace / ".gitignore").read_text().splitlines())
        self.assertEqual(self.git(workspace, "status", "--porcelain", "--untracked-files=all"), "")
        listed = self.mt("list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)

    # A workspace folder in the new layout that still has the old file name

    def test_a_folder_with_the_old_name_is_refused_and_nothing_is_created(self) -> None:
        folder = self.old_name_folder(self.root / "srv" / "alice", git=False)
        before = self.listing(folder)
        in_app = self.old_name_folder(self.default, git=False)
        in_app_before = self.listing(in_app)

        cases = [
            (("list",), {"MT_WORKSPACE": str(folder)}, str(folder), f"MT_WORKSPACE={folder}"),
            (("add", "Nope"), {"MT_WORKSPACE": str(folder)}, str(folder), "migrate"),
            (("validate",), {}, str(in_app), f"{self.app / 'mt'} migrate"),
            (("add", "Nope"), {}, str(in_app), "migrate"),
            (("-R", str(folder), "validate"), {}, str(folder), f"mt -R {folder} migrate"),
        ]
        for arguments, extra, shown, hint in cases:
            with self.subTest(arguments=arguments, extra=extra):
                result = self.mt(*arguments, env={**self.env, **extra})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("still uses mt-policy.yaml", result.stderr)
                self.assertIn(shown, result.stderr)
                self.assertIn(hint, result.stderr)
        self.assertEqual(self.listing(folder), before)
        self.assertEqual(self.listing(in_app), in_app_before)
        self.assertFalse((folder / "settings.yaml").exists())
        self.assertFalse((in_app / "settings.yaml").exists())

    def test_mt_init_does_not_create_over_a_folder_with_the_old_name(self) -> None:
        in_app = self.old_name_folder(self.default, git=False)
        before = self.listing(in_app)

        result = self.mt("init")

        self.assertEqual(result.returncode, 1)
        self.assertIn("still uses mt-policy.yaml", result.stderr)
        self.assertIn("migrate", result.stderr)
        self.assertEqual(self.listing(in_app), before)
        self.assertFalse((in_app / "settings.yaml").exists())

        folder = self.old_name_folder(self.root / "srv" / "bob", git=False)
        again = self.mt("init", str(folder), "--data")
        self.assertNotEqual(again.returncode, 0)
        self.assertFalse((folder / "settings.yaml").exists())

    def test_migrate_renames_and_commits_only_the_rename_in_a_git_folder(self) -> None:
        folder = self.old_name_folder(self.root / "srv" / "alice", git=True)
        self.git(self.root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.root / "origin.git"))
        self.git(folder, "remote", "add", "origin", str(self.root / "origin.git"))
        commits = int(self.git(folder, "rev-list", "--count", "HEAD"))
        before = self.listing(folder)

        checked = self.mt("-R", str(folder), "migrate", "--check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("Rename mt-policy.yaml to settings.yaml", checked.stdout)
        self.assertIn(RENAME_MESSAGE, checked.stdout)
        self.assertEqual(self.listing(folder), before)
        self.assertEqual(int(self.git(folder, "rev-list", "--count", "HEAD")), commits)

        result = self.mt("-R", str(folder), "migrate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing was pushed", result.stdout)
        self.assertEqual(int(self.git(folder, "rev-list", "--count", "HEAD")), commits + 1)
        self.assertEqual(self.git(folder, "log", "-1", "--format=%s"), RENAME_MESSAGE)
        status = self.git(folder, "show", "-M", "--name-status", "--format=", "HEAD").splitlines()
        self.assertIn("R100\tmt-policy.yaml\tsettings.yaml", status)
        self.assertFalse((folder / "mt-policy.yaml").exists())
        self.assertFalse((folder / "mt-policy.local.yaml").exists())
        self.assertEqual((folder / "settings.local.yaml").read_text(), LOCAL)
        # The local file was tracked: it stays on disk but is no longer in Git, and is ignored.
        self.assertIn("D\tmt-policy.local.yaml", self.git(folder, "show", "--name-status", "--format=", "HEAD"))
        self.assertEqual(self.git(folder, "ls-files", "settings.local.yaml", "mt-policy.local.yaml"), "")
        self.assertIn("settings.local.yaml", (folder / ".gitignore").read_text().splitlines())
        self.assertEqual(self.git(folder, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(subprocess.run(["git", "-C", str(self.root / "origin.git"), "rev-parse", "--verify", "main"],
                                        env=self.env, capture_output=True).returncode, 128, "something was pushed")
        listed = self.mt("-R", str(folder), "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("Kept card", listed.stdout)

    def test_migrate_through_mt_workspace_and_without_git(self) -> None:
        folder = self.old_name_folder(self.root / "srv" / "bob", git=False, local=False)
        env = {**self.env, "MT_WORKSPACE": str(folder)}

        checked = self.mt("migrate", "--check", env=env)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("no Git", checked.stdout)
        self.assertTrue((folder / "mt-policy.yaml").exists())
        self.assertFalse((folder / "settings.yaml").exists())

        result = self.mt("migrate", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((folder / "settings.yaml").is_file())
        self.assertFalse((folder / "mt-policy.yaml").exists())
        self.assertFalse((folder / ".git").exists())
        self.assertIn("Kept card", self.mt("list", "--json", env=env).stdout)
        again = self.mt("migrate", env=env)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already uses settings.yaml", again.stderr)

    def test_migrate_renames_the_default_workspace_folder_of_an_app(self) -> None:
        folder = self.old_name_folder(self.default, git=True, local=False)

        result = self.mt("migrate")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((folder / "settings.yaml").is_file())
        self.assertEqual(self.git(folder, "log", "-1", "--format=%s"), RENAME_MESSAGE)
        self.assertEqual(self.mt("validate").returncode, 0)

    def test_migrate_refuses_a_dirty_git_folder_and_changes_nothing(self) -> None:
        folder = self.old_name_folder(self.root / "srv" / "alice", git=True)
        (folder / "cards" / "2026-01-02-090000-edited.md").write_text("---\ntitle: Edited\n---\n")
        before = self.listing(folder)
        head = self.git(folder, "rev-parse", "HEAD")

        result = self.mt("-R", str(folder), "migrate")

        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertEqual(self.listing(folder), before)
        self.assertEqual(self.git(folder, "rev-parse", "HEAD"), head)
        self.assertTrue((folder / "mt-policy.yaml").exists())


if __name__ == "__main__":
    unittest.main()
