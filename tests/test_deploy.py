from __future__ import annotations

import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = SOURCE_ROOT / "deploy" / "add-user.sh"
UPGRADE = SOURCE_ROOT / "deploy" / "upgrade.sh"
BASH = shutil.which("bash")


def tag_releases(source: Path) -> None:
    """Give a local clone two release tags, the second on a new commit."""
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    for command in (
        ["tag", "v1.0.0"],
        [*identity, "commit", "--quiet", "--allow-empty", "-m", "second release"],
        ["tag", "v1.1.0"],
    ):
        subprocess.run(["git", "-C", str(source), *command], check=True, capture_output=True, text=True)


@unittest.skipUnless(BASH, "bash is not available")
class AddUserScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name).resolve()
        # A local clone source keeps everything off the network. It gets two
        # release tags, the second on a new commit.
        cls.source = base / "source"
        subprocess.run(["git", "clone", "--quiet", str(SOURCE_ROOT), str(cls.source)],
                       check=True, capture_output=True, text=True)
        tag_releases(cls.source)
        # A checkout of the app, as add-user.sh finds it in /opt/microtask/current.
        cls.app = base / "app"
        subprocess.run(["git", "clone", "--quiet", str(cls.source), str(cls.app)],
                       check=True, capture_output=True, text=True)
        cls.base = base

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(dir=self.base))

    def add_user(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return subprocess.run(
            [BASH, str(SCRIPT), "--root", str(self.root), "--no-systemctl",
             "--app", str(self.app), *arguments],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

    def test_creates_workspace_env_site_and_email(self) -> None:
        result = self.add_user("alice", "Alice@Example.com", "alice.mt.example.com")

        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.root / "srv" / "mt" / "alice"
        self.assertTrue((workspace / "cards").is_dir())
        self.assertTrue((workspace / ".git").is_dir())
        self.assertFalse((workspace / "mt").exists())
        self.assertFalse((workspace / "scripts").exists())
        remotes = subprocess.run(["git", "-C", str(workspace), "remote"],
                                 check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(remotes, [])
        log = subprocess.run(["git", "-C", str(workspace), "log", "--oneline"],
                             check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(log), 1)
        policy = (workspace / "mt-policy.yaml").read_text()
        self.assertIn("auto_sync: false", policy)

        self.assertEqual((self.root / "etc/mt/alice.env").read_text(),
                         "PORT=8101\nHOST_NAME=alice.mt.example.com\n")
        site = (self.root / "etc/caddy/mt-users/alice.caddy").read_text()
        self.assertIn("alice.mt.example.com {", site)
        self.assertIn("not header X-Auth-Request-Email alice@example.com", site)
        self.assertIn("reverse_proxy 127.0.0.1:8101", site)
        self.assertIn("request_header -X-Auth-Request-Email", site)
        self.assertIn("forward_auth 127.0.0.1:4180", site)
        self.assertNotIn("@USER@", site)
        self.assertNotIn("@PORT@", site)
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(), "alice@example.com\n")

    def test_second_user_gets_next_port_and_email_is_appended(self) -> None:
        self.assertEqual(self.add_user("alice", "alice@example.com", "alice.mt.example.com").returncode, 0)
        result = self.add_user("bob", "bob@example.com", "bob.mt.example.com")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/mt/bob.env").read_text(),
                         "PORT=8102\nHOST_NAME=bob.mt.example.com\n")
        self.assertIn("reverse_proxy 127.0.0.1:8102",
                      (self.root / "etc/caddy/mt-users/bob.caddy").read_text())
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(),
                         "alice@example.com\nbob@example.com\n")

    def test_duplicate_user_is_refused_and_changes_nothing(self) -> None:
        self.assertEqual(self.add_user("alice", "alice@example.com", "alice.mt.example.com").returncode, 0)
        env_before = (self.root / "etc/mt/alice.env").read_text()

        result = self.add_user("alice", "other@example.com", "other.mt.example.com")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual((self.root / "etc/mt/alice.env").read_text(), env_before)
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(), "alice@example.com\n")

    def test_invalid_names_hosts_and_emails_are_refused(self) -> None:
        for arguments in (
            ("Alice", "alice@example.com", "alice.mt.example.com"),
            ("-alice", "alice@example.com", "alice.mt.example.com"),
            ("al_ice", "alice@example.com", "alice.mt.example.com"),
            ("../alice", "alice@example.com", "alice.mt.example.com"),
            ("alice", "not-an-email", "alice.mt.example.com"),
            ("alice", "alice@example.com", "localhost"),
            ("alice", "alice@example.com", "bad host.example.com"),
        ):
            with self.subTest(arguments=arguments):
                result = self.add_user(*arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("error:", result.stderr)
        self.assertFalse((self.root / "srv").exists())
        self.assertFalse((self.root / "etc").exists())

    def test_dry_run_changes_nothing(self) -> None:
        env = dict(os.environ, HOME=str(self.root))
        result = subprocess.run(
            [BASH, str(SCRIPT), "--root", str(self.root), "--no-systemctl", "--dry-run",
             "--app", str(self.app),
             "alice", "alice@example.com", "alice.mt.example.com"],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORT=8101", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_script_syntax_and_shellcheck(self) -> None:
        syntax = subprocess.run([BASH, "-n", str(SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        lint = subprocess.run([shellcheck, str(SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)


@unittest.skipUnless(BASH, "bash is not available")
class UpgradeScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name).resolve()
        cls.source = base / "source"
        subprocess.run(["git", "clone", "--quiet", str(SOURCE_ROOT), str(cls.source)],
                       check=True, capture_output=True, text=True)
        tag_releases(cls.source)
        cls.base = base

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(dir=self.base))
        self.apps = self.root / "opt" / "microtask"

    def environment(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return env

    def upgrade(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(UPGRADE), "--root", str(self.root), "--no-systemctl",
             "--source", str(self.source), *arguments],
            check=False, capture_output=True, text=True, env=self.environment(), stdin=subprocess.DEVNULL,
        )

    def add_user(self, name: str) -> None:
        result = subprocess.run(
            [BASH, str(SCRIPT), "--root", str(self.root), "--no-systemctl",
             "--app", str(self.apps / "current"), name, f"{name}@example.com", f"{name}.mt.example.com"],
            check=False, capture_output=True, text=True, env=self.environment(), stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def link(self, name: str) -> str | None:
        path = self.apps / name
        return os.readlink(path) if path.is_symlink() else None

    def test_first_install_creates_version_and_current_only(self) -> None:
        result = self.upgrade("v1.0.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.apps / "v1.0.0" / "mt").is_file())
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertIsNone(self.link("previous"))
        self.assertIn("v1.0.0", result.stdout)
        self.assertEqual([path.name for path in self.apps.iterdir() if path.name.startswith(".")], [])
        mode = (self.apps / "v1.0.0" / "mt").stat().st_mode
        self.assertFalse(mode & (stat.S_IWGRP | stat.S_IWOTH))

    def test_upgrade_switches_links_and_validates_every_workspace(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        self.add_user("bob")

        result = self.upgrade("v1.1.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link("current"), "v1.1.0")
        self.assertEqual(self.link("previous"), "v1.0.0")
        self.assertIn("ok: alice", result.stdout)
        self.assertIn("ok: bob", result.stdout)
        self.assertTrue((self.apps / "v1.0.0").is_dir())

    def test_failing_workspace_stops_the_switch(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        self.add_user("bob")
        (self.root / "srv/mt/bob/cards/broken.md").write_text("no front matter here\n")

        result = self.upgrade("v1.1.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bob", result.stderr)
        self.assertNotIn("FAILED: alice", result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertIsNone(self.link("previous"))
        self.assertTrue((self.apps / "v1.1.0").is_dir())
        self.assertIn("stays in", result.stderr)

    def test_failed_clone_leaves_nothing_behind(self) -> None:
        result = self.upgrade("v9.9.9")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not clone", result.stderr)
        self.assertEqual(list(self.apps.iterdir()), [])

    def test_rollback_swaps_back_and_forth(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.assertEqual(self.upgrade("v1.1.0").returncode, 0)

        result = self.upgrade("--rollback")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertEqual(self.link("previous"), "v1.1.0")
        self.assertEqual(self.upgrade("--rollback").returncode, 0)
        self.assertEqual(self.link("current"), "v1.1.0")

    def test_rollback_without_previous_is_refused(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)

        result = self.upgrade("--rollback")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no previous", result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")

    def test_current_tag_is_a_no_op(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)

        result = self.upgrade("v1.0.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already the current", result.stdout)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertIsNone(self.link("previous"))

    def test_existing_non_current_folder_is_refused(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.assertEqual(self.upgrade("v1.1.0").returncode, 0)

        result = self.upgrade("v1.0.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.link("current"), "v1.1.0")

    def test_dry_run_and_bad_tags_change_nothing(self) -> None:
        self.assertEqual(self.upgrade("--dry-run", "v1.0.0").returncode, 0)
        for tag in ("../x", "current", "bad tag"):
            with self.subTest(tag=tag):
                self.assertNotEqual(self.upgrade(tag).returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def snapshot(self, folder: Path) -> dict[str, tuple[int, int]]:
        found = {}
        for directory, names, files in os.walk(folder):
            for name in [*names, *files]:
                path = Path(directory) / name
                info = path.lstat()
                found[str(path)] = (info.st_mtime_ns, info.st_size)
        return found

    def test_read_only_app_runs_a_data_only_workspace(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        app = self.apps / "v1.0.0"
        workspace = self.root / "srv/mt/alice"
        for path in [app, *app.rglob("*")]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

        def restore() -> None:
            for path in [app, *app.rglob("*")]:
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | stat.S_IWUSR)
        self.addCleanup(restore)

        env = self.environment()
        env.update(HOME=str(workspace), PYTHONDONTWRITEBYTECODE="1")
        before = self.snapshot(app)

        def mt(*arguments: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run([str(app / "mt"), "-R", str(workspace), *arguments], cwd=workspace,
                                    check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 0, f"{arguments}: {result.stderr}")
            return result

        mt("add", "Read only app task")
        self.assertIn("Read only app task", mt("list").stdout)
        mt("time", "start", "Read only")
        mt("time", "stop")
        mt("validate")

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen(
            [str(app / "mt"), "-R", str(workspace), "web", "--no-open", "--host", "127.0.0.1",
             "--port", str(port), "--allow-host", "alice.mt.example.com"],
            cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"Host": "alice.mt.example.com"})
            for _ in range(50):
                try:
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail("the dashboard did not start")
        finally:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

        self.assertEqual(self.snapshot(app), before)

    def test_script_syntax_and_shellcheck(self) -> None:
        syntax = subprocess.run([BASH, "-n", str(UPGRADE)], check=False, capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        lint = subprocess.run([shellcheck, str(UPGRADE)], check=False, capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)


class DeployFilesTests(unittest.TestCase):
    def test_kit_has_no_real_hosts_or_secrets(self) -> None:
        text = "\n".join(path.read_text() for path in (SOURCE_ROOT / "deploy").rglob("*") if path.is_file())
        self.assertIn("example.com", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertNotRegex(text, r"[A-Za-z0-9._-]+@(?!example\.com)[a-z0-9-]+\.(com|org|net|io)\b")


if __name__ == "__main__":
    unittest.main()
