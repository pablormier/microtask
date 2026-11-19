"""install.sh: release download, workspace creation, shell config, and origin handling."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import make_release


SOURCE_ROOT = Path(__file__).resolve().parents[1]
TAG = "v0.2.0"


class InstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.archive = make_release(self.root / "release.tar.gz", TAG)
        self.installer = self.root / "install.sh"
        shutil.copy2(SOURCE_ROOT / "install.sh", self.installer)
        (self.root / "gitconfig").write_text("[user]\n\tname = Install Test\n\temail = install@example.invalid\n")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("MT_") and key not in {"GIT_DIR", "GIT_WORK_TREE"}}
        self.env.update({
            "HOME": str(self.home),
            "SHELL": "/bin/zsh",
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "MT_UPSTREAM_URL": str(self.root / "app-repository.git"),
            "MT_RELEASE_TAG": TAG,
            "MT_ARCHIVE_URL": self.archive.as_uri(),
        })
        self.zshrc = self.home / ".zshrc"
        self.app = self.home / "microtask"

    def install(self, *arguments: str, installer: Path | None = None, env: dict[str, str] | None = None,
                cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        # A new session has no controlling terminal, as under cron or CI: the defaults are used.
        return subprocess.run(["bash", str(installer or self.installer), *arguments], env=env or self.env,
                              cwd=cwd or self.root, text=True, capture_output=True, check=False,
                              stdin=subprocess.DEVNULL, start_new_session=True)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], env=self.env, text=True, capture_output=True,
                              check=True).stdout.strip()

    def make_remote(self, name: str, *, workspace: bool, legacy: bool = False, old_name: bool = False) -> Path:
        """A bare repository: empty, or holding a workspace (old-style when legacy)."""
        bare = self.root / name
        if not workspace:
            self.git("init", "--quiet", "--bare", "--initial-branch=main", str(bare))
            return bare
        source = self.root / f"{name}-source"
        subprocess.run(["python3", str(SOURCE_ROOT / "scripts" / "workspace.py"), "create", str(source)],
                       check=True, capture_output=True)
        (source / "cards" / "2026-01-01-090000-from-remote.md").write_text(
            "---\ntitle: From remote\nstatus: inbox\npriority: none\ntype: task\ncreated: 2026-01-01T09:00:00\n---\n")
        if legacy:
            (source / "mt").write_text("#!/bin/sh\n")
        if old_name:
            (source / "settings.yaml").rename(source / "mt-policy.yaml")
        self.git("init", "--quiet", "--initial-branch=main", str(source))
        self.git("-C", str(source), "add", "-A")
        self.git("-C", str(source), "commit", "--quiet", "-m", "workspace")
        self.git("clone", "--quiet", "--bare", str(source), str(bare))
        return bare

    def test_without_a_forced_tag_the_newest_published_release_is_installed(self) -> None:
        listed = [{"tag_name": name, "draft": draft, "prerelease": pre} for name, draft, pre in (
            ("v0.2.1", False, False), ("v0.10.0", False, False), ("v0.9.0", False, False),
            ("v0.11.0", True, False), ("v0.12.0", False, True), ("nightly", False, False))]
        releases = self.root / "releases.json"
        releases.write_text(__import__("json").dumps(listed))
        env = {key: value for key, value in self.env.items() if key != "MT_RELEASE_TAG"}
        env["MT_RELEASES_URL"] = releases.as_uri()

        result = self.install(env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / "VERSION").read_text().strip(), "v0.10.0")

    def test_defaults_without_a_terminal_install_the_app_and_a_workspace_inside_it(self) -> None:
        result = self.install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.app / "VERSION").read_text().strip(), TAG)
        recorded = (self.app / ".release-files").read_text().split()
        self.assertIn("mt", recorded)
        self.assertIn("scripts", recorded)
        self.assertNotIn("VERSION", recorded)
        self.assertNotIn("workspace", recorded)
        self.assertTrue(os.access(self.app / "mt", os.X_OK))
        self.assertFalse((self.app / ".git").exists())
        workspace = self.app / "workspace"
        self.assertTrue((workspace / "cards").is_dir())
        self.assertTrue((workspace / "settings.yaml").is_file())
        self.assertIn(".mt/", (workspace / ".gitignore").read_text())
        for target in (".agents/skills", ".claude/skills"):
            self.assertTrue((workspace / target / "microtask/SKILL.md").is_file())
        self.assertIn(f"microtask {TAG} installed", result.stdout)
        self.assertIn("  app:        ~/microtask", result.stdout)
        self.assertIn("  workspace:  ~/microtask/workspace", result.stdout)
        self.assertIn("  update:     mt update", result.stdout)
        self.assertIn("source ~/.zshrc", result.stdout)
        path_line = f'export PATH="{self.app}:$PATH"'
        self.assertEqual(self.zshrc.read_text().count(path_line), 1)
        self.assertNotIn("MT_WORKSPACE", self.zshrc.read_text())

        # Running it again from the installed copy downloads nothing and adds nothing.
        before = self.zshrc.read_text()
        again = self.install("--here", installer=self.app / "install.sh", env={**self.env, "MT_ARCHIVE_URL": "file:///missing"})
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.zshrc.read_text(), before)
        self.assertIn("is set up", again.stdout)

    def test_custom_workspace_is_recorded_once_as_mt_workspace(self) -> None:
        workspace = self.root / "data" / "my cards"
        for _ in range(2):
            result = self.install("--workspace", str(workspace))
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue((workspace / "settings.yaml").is_file())
        self.assertFalse((self.app / "workspace").exists())
        text = self.zshrc.read_text()
        self.assertEqual(text.count(f'export MT_WORKSPACE="{workspace}"'), 1)
        self.assertEqual(text.count(f'export PATH="{self.app}:$PATH"'), 1)
        added = subprocess.run([str(self.app / "mt"), "add", "Elsewhere"], env={**self.env, "MT_WORKSPACE": str(workspace)},
                               text=True, capture_output=True, check=False, stdin=subprocess.DEVNULL)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual(len(list((workspace / "cards").glob("*.md"))), 1)
        self.assertFalse((self.app / "workspace").exists())

    def test_workspace_and_origin_variables_in_the_environment_are_ignored(self) -> None:
        origin = self.make_remote("env-origin.git", workspace=False)
        env = {**self.env, "MT_WORKSPACE": str(self.root / "from-env"), "MT_ORIGIN": str(origin)}

        result = self.install(env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / "workspace" / "settings.yaml").is_file())
        self.assertFalse((self.root / "from-env").exists())
        self.assertNotIn("MT_WORKSPACE", self.zshrc.read_text())
        self.assertFalse((self.app / "workspace" / ".git").exists())
        self.assertEqual(self.git("ls-remote", "--heads", str(origin)), "")

    def test_questions_are_read_from_the_terminal_and_enter_accepts_the_default(self) -> None:
        answers = self.root / "answers"
        app = self.root / "chosen app"
        workspace = self.root / "chosen cards"
        answers.write_text(f"{app}\n{workspace}\n\nn\n")

        result = self.install(env={**self.env, "MT_INSTALL_TTY": str(answers)})

        self.assertEqual(result.returncode, 0, result.stderr)
        for prompt in ("Install microtask in [~/microtask]:", "Workspace folder [", "Private git repo to sync cards (optional, Enter to skip):",
                       "Add mt to ~/.zshrc? [Y/n]:"):
            self.assertIn(prompt, result.stderr)
        self.assertTrue((app / "mt").is_file())
        self.assertTrue((workspace / "cards").is_dir())
        # Declining PATH still saves the workspace location, or mt would not find the cards later.
        text = self.zshrc.read_text()
        self.assertEqual(text.count(f'export MT_WORKSPACE="{workspace}"'), 1)
        self.assertNotIn("export PATH=", text)
        self.assertIn("The workspace location was saved in ~/.zshrc", result.stdout)
        added = subprocess.run([str(app / "mt"), "add", "Found"], env={**self.env, "MT_WORKSPACE": str(workspace)},
                               text=True, capture_output=True, check=False, stdin=subprocess.DEVNULL)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertFalse((app / "workspace").exists())
        self.zshrc.unlink()

        default = self.root / "enter"
        default.write_text("\n\n\n\n")
        result = self.install(env={**self.env, "MT_INSTALL_TTY": str(default)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / "workspace" / "cards").is_dir())
        self.assertEqual(self.zshrc.read_text().count(f'export PATH="{self.app}:$PATH"'), 1)

    def test_no_path_leaves_the_shell_config_alone(self) -> None:
        self.zshrc.write_text("# mine\n")
        for arguments, extra in ((("--no-path",), {}), ((), {"MT_NO_PATH": "1"})):
            target = self.root / f"extra-{len(extra)}"
            result = self.install(*arguments, "--dir", str(target), env={**self.env, **extra})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.zshrc.read_text(), "# mine\n")
            self.assertIn(f"{target}/mt", result.stdout)
        elsewhere = self.root / "cards elsewhere"
        result = self.install("--no-path", "--dir", str(self.root / "extra-ws"), "--workspace", str(elsewhere))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.zshrc.read_text(), "# mine\n")
        self.assertIn(f'export MT_WORKSPACE="{elsewhere}"', result.stdout)
        unknown = self.install("--no-pth")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown option --no-pth", unknown.stderr)

    def test_positional_dir_and_origin_still_work(self) -> None:
        remote = self.make_remote("tasks.git", workspace=False)
        destination = self.root / "positional"

        result = self.install(str(destination), str(remote))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("-C", str(destination / "workspace"), "remote", "get-url", "origin"), str(remote))
        self.assertIn("settings.yaml", self.git("--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"))
        self.assertEqual(self.git("-C", str(destination / "workspace"), "status", "--porcelain"), "")

    def test_origin_that_holds_a_workspace_is_cloned_into_the_workspace_folder(self) -> None:
        remote = self.make_remote("mine.git", workspace=True)

        result = self.install("--origin", str(remote))

        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.app / "workspace"
        self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "origin"), str(remote))
        self.assertTrue((workspace / "cards" / "2026-01-01-090000-from-remote.md").is_file())
        self.assertFalse((self.app / ".git").exists())

    def test_origin_with_the_old_layout_is_refused(self) -> None:
        remote = self.make_remote("old.git", workspace=True, legacy=True)

        result = self.install("--origin", str(remote))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("old layout", result.stderr)
        self.assertFalse((self.app / "workspace").exists())

    def test_origin_that_still_uses_the_old_settings_name_is_refused(self) -> None:
        remote = self.make_remote("unmigrated.git", workspace=True, old_name=True)

        result = self.install("--origin", str(remote))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mt-policy.yaml", result.stderr)
        self.assertIn("mt migrate", result.stderr)
        self.assertFalse((self.app / "workspace").exists())

    def test_refuses_the_app_repository_as_origin_before_downloading(self) -> None:
        remote = self.make_remote("app-repository.git", workspace=True)

        result = self.install("--origin", f"{remote}/")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("application repository", result.stderr)
        self.assertFalse(self.app.exists())

    def test_refuses_a_public_github_origin_when_gh_reports_it(self) -> None:
        empty = self.make_remote("empty.git", workspace=False)
        with open(self.root / "gitconfig", "a") as config:
            config.write(f'[url "{empty}"]\n\tinsteadOf = https://github.com/me/tasks.git\n')
        binary = self.root / "bin"
        binary.mkdir()
        (binary / "gh").write_text("#!/bin/sh\necho PUBLIC\n")
        (binary / "gh").chmod(0o755)
        env = {**self.env, "PATH": f"{binary}{os.pathsep}{self.env['PATH']}"}

        result = self.install("--origin", "https://github.com/me/tasks.git", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("public GitHub repository", result.stderr)
        self.assertFalse(self.app.exists())
        self.assertEqual(self.git("--git-dir", str(empty), "for-each-ref"), "")

    def test_unreadable_origin_stops_before_downloading(self) -> None:
        result = self.install("--origin", str(self.root / "missing.git"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read", result.stderr)
        self.assertFalse(self.app.exists())

    def test_unpacked_copy_is_set_up_without_downloading(self) -> None:
        copy = self.root / "unpacked"
        shutil.unpack_archive(self.archive, self.root / "unpack-tmp")
        shutil.move(str(next((self.root / "unpack-tmp").iterdir())), copy)

        refused = self.install(installer=copy / "install.sh", env={**self.env, "MT_ARCHIVE_URL": "file:///missing"})
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--here", refused.stderr)
        self.assertFalse((copy / "workspace").exists())
        self.assertFalse(self.zshrc.exists())

        result = self.install("--here", installer=copy / "install.sh",
                              env={**self.env, "MT_ARCHIVE_URL": "file:///missing"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((copy / "workspace" / "cards").is_dir())
        self.assertIn(f'export PATH="{copy}:$PATH"', self.zshrc.read_text())
        self.assertNotIn("Downloading", result.stderr)

    def test_here_sets_up_a_git_clone_like_any_app_folder(self) -> None:
        clone = self.root / "clone"
        shutil.unpack_archive(self.archive, self.root / "unpack-tmp")
        shutil.move(str(next((self.root / "unpack-tmp").iterdir())), clone)
        (clone / ".git").mkdir()

        result = self.install("--here", "--no-path", installer=clone / "install.sh",
                              env={**self.env, "MT_ARCHIVE_URL": "file:///missing"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((clone / "workspace" / "cards").is_dir())

    def test_mt_repo_no_longer_chooses_the_folder(self) -> None:
        elsewhere = self.root / "from-mt-repo"

        result = self.install(env={**self.env, "MT_REPO": str(elsewhere)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / "mt").is_file())
        self.assertFalse(elsewhere.exists())

    def test_here_needs_a_microtask_folder_and_excludes_dir(self) -> None:
        result = self.install("--here")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--here needs install.sh to be run from inside a microtask folder", result.stderr)
        self.assertFalse(self.app.exists())

    def test_old_style_install_keeps_its_cards_and_gets_no_second_workspace(self) -> None:
        shutil.unpack_archive(self.archive, self.root / "unpack-tmp")
        old = self.root / "old"
        shutil.move(str(next((self.root / "unpack-tmp").iterdir())), old)
        (old / "cards").mkdir()
        shutil.copy2(old / "templates" / "settings.yaml", old / "settings.yaml")

        result = self.install("--here", installer=old / "install.sh")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((old / "workspace").exists())
        self.assertIn("mt migrate", result.stdout)

    def test_other_installs_on_path_are_reported(self) -> None:
        older = self.home / "old-todo"
        (older / "scripts").mkdir(parents=True)
        (older / "scripts" / "cli.py").write_text("")
        (older / "mt").write_text("#!/usr/bin/env bash\nexit 0\n")
        (older / "mt").chmod(0o755)
        older_line = 'export PATH="$HOME/old-todo:$PATH"'
        self.zshrc.write_text(f"# microtask\n{older_line}\n")

        result = self.install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(older), result.stdout)
        self.assertEqual(self.zshrc.read_text().splitlines()[-1], f'export PATH="{self.app}:$PATH"')

        self.zshrc.write_text(self.zshrc.read_text() + f"{older_line}\n")
        result = self.install()
        self.assertIn(f"puts mt from {older} on PATH after this one", result.stderr)

    def test_existing_unrelated_folders_are_never_overwritten(self) -> None:
        self.app.mkdir()
        (self.app / "mine.txt").write_text("keep\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual((self.app / "mine.txt").read_text(), "keep\n")

        elsewhere = self.root / "cards-here"
        elsewhere.mkdir()
        (elsewhere / "note.txt").write_text("keep\n")
        result = self.install("--dir", str(self.root / "fresh"), "--workspace", str(elsewhere))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a microtask workspace", result.stderr)
        self.assertFalse((self.root / "fresh").exists())

    def test_unsafe_archive_does_not_publish_the_destination(self) -> None:
        unsafe = make_release(self.root / "unsafe.tar.gz", TAG, raw_members=[("microtask-0.2.0/../../escape", b"x\n", 0o644)])

        result = self.install(env={**self.env, "MT_ARCHIVE_URL": unsafe.as_uri()})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe archive path", result.stderr)
        self.assertFalse(self.app.exists())
        self.assertFalse((self.root.parent / "escape").exists())


if __name__ == "__main__":
    unittest.main()
