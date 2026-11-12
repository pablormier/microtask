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
        (source / "scripts" / "tasklib.py").write_text("")
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

    def test_clones_existing_origin_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = self.make_workspace_remote(root)
            env = self.setup_env(root, remote)
            installer = root / "install.sh"
            shutil.copy2(SOURCE_ROOT / "install.sh", installer)
            workspace = root / "todo"

            result = subprocess.run([str(installer), str(workspace), str(remote)], env=env,
                                    text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "origin"), str(remote))
            self.assertEqual(self.git("-C", str(workspace), "remote", "get-url", "upstream"), str(remote))
            self.assertNotIn("push", result.stderr)

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
