import os
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class InstallTest(unittest.TestCase):
    def write_archive(self, archive_path: Path, *, unsafe: bool = False) -> None:
        with tarfile.open(archive_path, "w:gz") as archive:
            entries = [
                ("microtask-main/mt", b"#!/usr/bin/env python3\n", 0o755),
                ("microtask-main/install.sh", b"#!/usr/bin/env bash\n", 0o755),
            ]
            if unsafe:
                entries.append(("microtask-main/../../escape", b"unsafe\n", 0o644))
            for name, contents, mode in entries:
                info = tarfile.TarInfo(name)
                info.size = len(contents)
                info.mode = mode
                archive.addfile(info, io.BytesIO(contents))

    def no_git_env(self, root: Path, archive_path: Path) -> dict[str, str]:
        binary = root / "bin"
        binary.mkdir()
        (binary / "bash").symlink_to("/bin/bash")
        (binary / "python3").symlink_to(sys.executable)
        env = os.environ.copy()
        env.pop("MT_REPO", None)
        env.pop("MT_ORIGIN", None)
        env.update({
            "HOME": str(root),
            "PATH": str(binary),
            "MT_ARCHIVE_URL": archive_path.as_uri(),
        })
        return env

    def test_clones_default_destination_with_upstream_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "bin"
            binary.mkdir()
            log = root / "git.log"
            git = binary / "git"
            git.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$GIT_LOG\"\n"
                "if [[ $1 == clone ]]; then mkdir -p \"${!#}\"; fi\n"
            )
            git.chmod(0o755)

            env = os.environ.copy()
            env.pop("MT_REPO", None)
            env.update(
                {
                    "GIT_LOG": str(log),
                    "HOME": str(root),
                    "PATH": f"{binary}:{env['PATH']}",
                }
            )
            installer = root / "install.sh"
            shutil.copy2(SOURCE_ROOT / "install.sh", installer)
            result = subprocess.run(
                [str(installer)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            destination = root / "todo"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                log.read_text().splitlines(),
                [
                    "clone --origin upstream "
                    f"https://github.com/pablormier/microtask.git {destination}",
                ],
            )
            self.assertIn(f"Installed microtask in {destination}", result.stdout)

    def test_refuses_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "existing"
            destination.mkdir()
            result = subprocess.run(
                [str(SOURCE_ROOT / "install.sh"), str(destination)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a microtask workspace", result.stderr)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], text=True, capture_output=True, check=True).stdout.strip()

    def make_workspace_remote(self, root: Path) -> Path:
        """Return a bare repository holding a minimal microtask workspace."""
        source = root / "source"
        (source / "scripts").mkdir(parents=True)
        (source / "scripts" / "cli.py").write_text("")
        (source / "mt").write_text("#!/usr/bin/env bash\nexit 0\n")
        (source / "mt").chmod(0o755)
        shutil.copy2(SOURCE_ROOT / "install.sh", source / "install.sh")
        self.git("init", "--quiet", str(source))
        self.git("-C", str(source), "add", "-A")
        self.git("-C", str(source), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                 "commit", "--quiet", "-m", "workspace")
        bare = root / "remote.git"
        self.git("clone", "--quiet", "--bare", str(source), str(bare))
        return bare

    def setup_env(self, root: Path, upstream: Path) -> dict[str, str]:
        env = os.environ.copy()
        for name in ("MT_REPO", "MT_ORIGIN", "GIT_DIR", "GIT_WORK_TREE"):
            env.pop(name, None)
        env.update({
            "HOME": str(root / "home"),
            "SHELL": "/bin/zsh",
            "MT_UPSTREAM_URL": str(upstream),
            "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        (root / "home").mkdir()
        return env

    def test_sets_up_cloned_workspace_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = self.make_workspace_remote(root)
            env = self.setup_env(root, remote)
            workspace = root / "mytodos"
            self.git("clone", "--quiet", str(remote), str(workspace))

            for _ in range(2):
                result = subprocess.run([str(workspace / "install.sh")], env=env, cwd=root,
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

            self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "upstream"), str(remote))
            self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "origin"), str(remote))
            zshrc = (root / "home" / ".zshrc").read_text()
            self.assertEqual(zshrc.count(f'export PATH="{workspace}:$PATH"'), 1)
            self.assertIn("git config --global user.name", result.stdout)
            self.assertIn(f"microtask is ready in {workspace}", result.stdout)

    def test_no_path_sets_up_an_extra_workspace_without_touching_the_shell_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = self.make_workspace_remote(root)
            env = self.setup_env(root, remote)
            workspace = root / "project-tasks"
            self.git("clone", "--quiet", str(remote), str(workspace))
            zshrc = root / "home" / ".zshrc"
            zshrc.write_text('# microtask\nexport PATH="$HOME/todo:$PATH"\n')
            before = zshrc.read_text()

            for command, extra in (([str(workspace / "install.sh"), "--no-path"], {}),
                                   ([str(workspace / "install.sh")], {"MT_NO_PATH": "1"})):
                result = subprocess.run(command, env={**env, **extra}, cwd=root,
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(zshrc.read_text(), before)
                self.assertIn(f"{workspace}/mt list", result.stdout)
                self.assertIn(f"mt repo add NAME {workspace}", result.stdout)
                self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "upstream"), str(remote))

            result = subprocess.run([str(workspace / "install.sh"), "--no-pth"], env=env, cwd=root,
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown option --no-pth", result.stderr)

    def make_local_workspace(self, folder: Path) -> Path:
        (folder / "scripts").mkdir(parents=True)
        (folder / "scripts" / "cli.py").write_text("")
        (folder / "mt").write_text("#!/usr/bin/env bash\nexit 0\n")
        (folder / "mt").chmod(0o755)
        return folder

    def test_reports_other_workspaces_on_path_and_which_one_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            remote = self.make_workspace_remote(root)
            env = self.setup_env(root, remote)
            older = self.make_local_workspace(root / "home" / "old-todo")
            workspace = root / "mytodos"
            self.git("clone", "--quiet", str(remote), str(workspace))
            zshrc = root / "home" / ".zshrc"
            older_line = 'export PATH="$HOME/old-todo:$PATH"'
            zshrc.write_text(f"# microtask\n{older_line}\n")

            result = subprocess.run([str(workspace / "install.sh")], env=env, cwd=root,
                                    text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(older), result.stdout)
            self.assertIn(f"New terminals use {workspace}", result.stdout)
            lines = zshrc.read_text().splitlines()
            self.assertEqual(lines[-1], f'export PATH="{workspace}:$PATH"')

            # A later line for the older workspace wins again; say so.
            zshrc.write_text(zshrc.read_text() + f"{older_line}\n")
            result = subprocess.run([str(workspace / "install.sh")], env=env, cwd=root,
                                    text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"puts mt from {older} on PATH after this workspace", result.stderr)

            env["PATH"] = f"{older}{os.pathsep}{env['PATH']}"
            result = subprocess.run([str(workspace / "install.sh")], env=env, cwd=root,
                                    text=True, capture_output=True, check=False)
            self.assertIn(f"This terminal currently runs mt from {older}", result.stdout)

    def test_clones_existing_origin_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = self.make_workspace_remote(root)
            upstream = root / "upstream.git"
            env = self.setup_env(root, upstream)
            installer = root / "install.sh"
            shutil.copy2(SOURCE_ROOT / "install.sh", installer)
            workspace = root / "todo"

            result = subprocess.run([str(installer), str(workspace), str(remote)], env=env,
                                    text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "origin"), str(remote))
            self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "upstream"), str(upstream))
            self.assertNotIn("push", result.stderr)

    def test_refuses_app_repository_as_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = self.make_workspace_remote(root)
            env = self.setup_env(root, remote)
            workspace = root / "todo"

            result = subprocess.run([str(SOURCE_ROOT / "install.sh"), str(workspace), str(remote) + "/"],
                                    env=env, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("application repository", result.stderr)
            self.assertFalse(workspace.exists())

    def test_refuses_public_github_origin_when_gh_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            upstream = self.make_workspace_remote(root)
            empty = root / "empty.git"
            self.git("init", "--quiet", "--bare", str(empty))
            env = self.setup_env(root, upstream)
            (root / "gitconfig").write_text(f'[url "{empty}"]\n\tinsteadOf = https://github.com/me/tasks.git\n')
            binary = root / "bin"
            binary.mkdir()
            (binary / "gh").write_text("#!/bin/sh\necho PUBLIC\n")
            (binary / "gh").chmod(0o755)
            env["PATH"] = f"{binary}{os.pathsep}{env['PATH']}"
            workspace = root / "todo"

            result = subprocess.run([str(SOURCE_ROOT / "install.sh"), str(workspace),
                                     "https://github.com/me/tasks.git"],
                                    env=env, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("public GitHub repository", result.stderr)
            self.assertFalse(workspace.exists())
            self.assertEqual(self.git("--git-dir", str(empty), "for-each-ref"), "")

    def test_unreadable_origin_stops_before_creating_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = self.setup_env(root, root / "missing-upstream.git")
            installer = root / "install.sh"
            shutil.copy2(SOURCE_ROOT / "install.sh", installer)
            workspace = root / "todo"

            result = subprocess.run([str(installer), str(workspace), str(root / "missing.git")], env=env,
                                    text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot read", result.stderr)
            self.assertFalse(workspace.exists())

    def test_installs_archive_without_git_and_preserves_executable_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "fixture.tar.gz"
            self.write_archive(archive_path)
            destination = root / "archive-workspace"
            env = self.no_git_env(root, archive_path)

            result = subprocess.run(
                [str(SOURCE_ROOT / "install.sh"), str(destination), "https://example.invalid/tasks.git"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((destination / "mt").is_file())
            self.assertTrue(os.access(destination / "mt", os.X_OK))
            self.assertFalse((destination / ".git").exists())
            self.assertIn("without Git", result.stdout)
            self.assertIn("git init", result.stdout)
            self.assertIn("git config user.name", result.stdout)
            self.assertIn("git commit", result.stdout)
            self.assertIn("git remote add origin https://example.invalid/tasks.git", result.stdout)

    def test_archive_failure_does_not_publish_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "unsafe.tar.gz"
            self.write_archive(archive_path, unsafe=True)
            destination = root / "should-not-exist"
            result = subprocess.run(
                [str(SOURCE_ROOT / "install.sh"), str(destination)],
                env=self.no_git_env(root, archive_path),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe archive path", result.stderr)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
