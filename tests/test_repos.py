from __future__ import annotations

import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class RepositorySelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve()
        self.launcher = self.home / "launcher"
        self.target = self.home / "target workspace"
        ignored = shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store")
        shutil.copytree(SOURCE_ROOT, self.launcher, ignore=ignored)
        shutil.copytree(SOURCE_ROOT, self.target, ignore=ignored)
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / "config"),
            }
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_mt(
        self,
        *arguments: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.launcher / "mt"), *arguments],
            cwd=cwd or self.home,
            env=env or self.env,
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )

    def test_run_uses_selected_workspace_and_forwards_arguments_io_and_status(self) -> None:
        added = self.run_mt("repo", "add", "work", str(self.target))
        self.assertEqual(added.returncode, 0, added.stderr)

        def state(workspace: Path) -> dict[str, bytes]:
            return {str(path.relative_to(workspace)): path.read_bytes()
                    for folder in ("cards", ".mt")
                    for path in (workspace / folder).rglob("*") if path.is_file()}

        before = {workspace: state(workspace) for workspace in (self.launcher, self.target)}
        probe = (
            "import json, os, sys; "
            "print(json.dumps([os.getcwd(), sys.argv[1:], sys.stdin.read(), os.environ['MT_RUN_TEST']])); "
            "print('child stderr', file=sys.stderr); sys.exit(23)"
        )
        arguments = ("two words", "", "--help", "-R", "other", "--", "café", "$(echo literal); *")
        env = {**self.env, "MT_RUN_TEST": "inherited"}
        for selector, expected in (((), self.launcher), (("-R", str(self.target)), self.target),
                                   (("--repo", "work"), self.target)):
            with self.subTest(selector=selector):
                result = self.run_mt(*selector, "run", "python3", "-c", probe, *arguments,
                                     env=env, input_text="child stdin\n")
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 [str(expected), list(arguments), "child stdin\n", "inherited"])
                self.assertEqual(result.stderr, "child stderr\n")
        for workspace in (self.launcher, self.target):
            self.assertEqual(state(workspace), before[workspace])

    def test_run_resolves_relative_executable_and_preserves_child_separator(self) -> None:
        executable = self.target / "probe command"
        executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$@\"\n", encoding="utf-8")
        executable.chmod(0o755)
        for separator in ((), ("--",)):
            with self.subTest(separator=separator):
                result = self.run_mt("-R", str(self.target), "run", *separator,
                                     "./probe command", "--", "--help", "two words")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{self.target}\n--\n--help\ntwo words\n")

    def test_run_rejects_missing_command_and_reports_launch_errors(self) -> None:
        for arguments in (("run",), ("run", "--"), ("run", "")):
            with self.subTest(arguments=arguments):
                result = self.run_mt(*arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("run requires a command", result.stderr)
        missing = self.run_mt("run", "./missing-executable")
        self.assertEqual(missing.returncode, 127, missing.stderr)
        self.assertIn("command not found", missing.stderr)
        blocked = self.launcher / "not-executable"
        blocked.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        blocked.chmod(0o644)
        denied = self.run_mt("run", "./not-executable")
        self.assertEqual(denied.returncode, 126, denied.stderr)
        self.assertIn("cannot run", denied.stderr)
        self.assertNotIn("Traceback", denied.stderr)
        self.assertFalse((self.launcher / ".mt").exists())

    def test_run_keeps_terminal_streams_and_signal_exit(self) -> None:
        master, slave = pty.openpty()
        try:
            terminal = subprocess.run(
                [str(self.launcher / "mt"), "run", sys.executable, "-c",
                 "import os, sys; sys.exit(0 if all(os.isatty(fd) for fd in (0, 1, 2)) else 3)"],
                cwd=self.home, env=self.env, stdin=slave, stdout=slave, stderr=slave,
                check=False, timeout=10,
            )
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual(terminal.returncode, 0)
        signaled = self.run_mt("run", sys.executable, "-c",
                               "import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(signaled.returncode, -signal.SIGTERM, signaled.stderr)

    def test_path_selectors_resolve_absolute_relative_and_home_paths(self) -> None:
        selectors = (str(self.target), "./target workspace", "~/target workspace")
        for selector in selectors:
            with self.subTest(selector=selector):
                result = self.run_mt("--repo", selector, "info")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("".join(str(self.target).split()),
                              "".join(result.stdout.replace("│", "").split()))

        short = self.run_mt("-R", str(self.target), "info")
        equals = self.run_mt(f"--repo={self.target}", "info")
        self.assertEqual(short.returncode, 0, short.stderr)
        self.assertEqual(equals.returncode, 0, equals.stderr)

    def test_selected_and_bare_commands_mutate_only_their_workspace(self) -> None:
        selected = self.run_mt("--repo", str(self.target), "add", "Selected workspace item")
        bare = self.run_mt("add", "Launcher workspace item")

        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(bare.returncode, 0, bare.stderr)
        launcher_text = "\n".join(path.read_text() for path in self.launcher.glob("cards/*.md"))
        target_text = "\n".join(path.read_text() for path in self.target.glob("cards/*.md"))
        self.assertIn("Launcher workspace item", launcher_text)
        self.assertNotIn("Selected workspace item", launcher_text)
        self.assertIn("Selected workspace item", target_text)
        self.assertNotIn("Launcher workspace item", target_text)

    def test_init_local_copies_the_application_but_never_workspace_data(self) -> None:
        source = self.launcher
        (source / "cards" / "2026-01-01-120000-private.md").write_text("SECRET-7f3a card\n")
        (source / "drafts").mkdir(exist_ok=True)
        (source / "drafts" / "private.md").write_text("SECRET-7f3a draft\n")
        (source / ".mt").mkdir(exist_ok=True)
        (source / ".mt" / "focus.json").write_text("{}")
        (source / "mt-policy.local.yaml").write_text("rules:\n  auto_sync: false\n")
        (source / "my-notes.txt").write_text("SECRET-7f3a notes\n")
        destination = self.home / "project tasks"

        result = self.run_mt("init", str(destination), "--local")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([path.name for path in (destination / "cards").iterdir()], [".gitkeep"])
        for private in ("drafts", ".mt/focus.json", "mt-policy.local.yaml", "my-notes.txt", ".git"):
            self.assertFalse((destination / private).exists(), private)
        self.assertTrue((destination / ".agents/skills/microtask/SKILL.md").is_file())
        self.assertEqual((destination / "mt-policy.yaml").read_text(), (source / "mt-policy.yaml").read_text())
        self.assertTrue(os.access(destination / "mt", os.X_OK))
        self.assertNotIn("SECRET-7f3a", "".join(p.read_text(errors="ignore") for p in destination.rglob("*") if p.is_file()))
        added = subprocess.run([str(destination / "mt"), "add", "Project task"], cwd=self.home,
                               env=self.env, text=True, capture_output=True, check=False)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.startswith(".project")], [])

        again = self.run_mt("init", str(destination), "--local")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already a microtask workspace", again.stdout)
        self.assertEqual(len(list((destination / "cards").glob("*.md"))), 1)
        other = self.home / "code"
        other.mkdir()
        refused = self.run_mt("init", str(other), "--local")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(list(other.iterdir()), [])
        self.assertEqual(self.run_mt("init", str(self.home / "x"), "git@example.com:me/x.git",
                                     "--local").returncode, 2)

    def test_init_without_local_runs_the_installer_without_changing_path(self) -> None:
        log = self.home / "install.log"
        installer = self.launcher / "install.sh"
        installer.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{log}"\n')

        result = self.run_mt("init", "~/project-tasks", "git@example.com:me/tasks.git")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(),
                         ["--no-path", str(self.home / "project-tasks"), "git@example.com:me/tasks.git"])

    def test_alias_lifecycle_and_stale_target_errors(self) -> None:
        added = self.run_mt("repo", "add", "work", str(self.target))
        duplicate = self.run_mt("repo", "add", "work", str(self.launcher))
        invalid = self.run_mt("repo", "add", "bad/name", str(self.target))
        listed = self.run_mt("repo", "list")
        selected = self.run_mt("--repo", "work", "info")

        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("already exists", duplicate.stderr)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("repository alias must", invalid.stderr)
        self.assertEqual(listed.stdout, f"work\t{self.target}\n")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertIn("".join(str(self.target).split()),
                      "".join(selected.stdout.replace("│", "").split()))

        moved = self.home / "moved target"
        self.target.rename(moved)
        stale = self.run_mt("--repo", "work", "list")
        self.assertEqual(stale.returncode, 2)
        self.assertIn("workspace does not exist", stale.stderr)

        removed = self.run_mt("repo", "remove", "work")
        missing = self.run_mt("repo", "remove", "work")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("unknown repository alias", missing.stderr)

    def test_aliases_are_sorted_and_unknown_alias_is_clear(self) -> None:
        second = self.home / "second"
        shutil.copytree(
            SOURCE_ROOT,
            second,
            ignore=shutil.ignore_patterns(".git", ".mt", "__pycache__", ".DS_Store"),
        )
        self.assertEqual(self.run_mt("repo", "add", "zeta", str(second)).returncode, 0)
        self.assertEqual(self.run_mt("repo", "add", "alpha", str(self.target)).returncode, 0)

        listed = self.run_mt("repo", "list")
        unknown = self.run_mt("--repo", "missing", "list")

        self.assertEqual(
            listed.stdout,
            f"alpha\t{self.target}\nzeta\t{second}\n",
        )
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("mt repo add missing PATH", unknown.stderr)

    def test_malformed_registry_and_invalid_workspace_are_rejected(self) -> None:
        registry = Path(self.env["XDG_CONFIG_HOME"]) / "microtask" / "repos.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("[]\n", encoding="utf-8")
        malformed = self.run_mt("repo", "list")
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("registry is malformed", malformed.stderr)

        registry.unlink()
        invalid = self.run_mt("--repo", str(self.home / "not-a-workspace"), "list")
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("workspace does not exist", invalid.stderr)

    def test_dispatch_forwards_capture_commands_unchanged(self) -> None:
        stub = self.target / "scripts" / "tasklib.py"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "print(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        cases = (
            (("add", "Plan dentist visit"), ["add", "Plan dentist visit"]),
            (("note", "https://example.com/article"), ["note", "https://example.com/article"]),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = self.run_mt("--repo", str(self.target), *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), expected)

    def test_selected_workspace_rejects_command_typos_without_creating_data(self) -> None:
        for command in (("import-githux",), ("ai",), ("batch", "prepare"),
                        ("artifact", "publish"), ("storage", "allocate-run")):
            result = self.run_mt("--repo", str(self.target), *command)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("invalid choice", result.stderr)
        self.assertFalse(list((self.target / "cards").glob("*.md")))
        for workspace in (self.launcher, self.target):
            self.assertFalse((workspace / ".mt").exists())
            self.assertFalse(list(workspace.glob("cards/*.md")))

    def test_help_documents_global_selector_without_claiming_import_repo(self) -> None:
        root_help = self.run_mt("--help")
        import_help = self.run_mt("import-github", "--help")

        self.assertEqual(root_help.returncode, 0, root_help.stderr)
        self.assertRegex(root_help.stdout, r"-R(?: PATH_OR_NAME)?, --repo PATH_OR_NAME")
        self.assertEqual(import_help.returncode, 0, import_help.stderr)
        self.assertIn("--repo OWNER/REPO", import_help.stdout)


if __name__ == "__main__":
    unittest.main()
