from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = SOURCE_ROOT / "deploy" / "add-user.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash is not available")
class AddUserScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name).resolve()
        # A local clone source keeps the workspace setup off the network.
        cls.source = base / "source"
        subprocess.run(["git", "clone", "--quiet", str(SOURCE_ROOT), str(cls.source)],
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
             "--source", str(self.source), *arguments],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

    def test_creates_workspace_env_site_and_email(self) -> None:
        result = self.add_user("alice", "Alice@Example.com", "alice.mt.example.com")

        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.root / "srv" / "mt" / "alice"
        self.assertTrue((workspace / "mt").is_file())
        self.assertTrue((workspace / "cards").is_dir())
        remotes = subprocess.run(["git", "-C", str(workspace), "remote"],
                                 check=True, capture_output=True, text=True).stdout.split()
        self.assertEqual(remotes, ["upstream"])
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


class DeployFilesTests(unittest.TestCase):
    def test_kit_has_no_real_hosts_or_secrets(self) -> None:
        text = "\n".join(path.read_text() for path in (SOURCE_ROOT / "deploy").rglob("*") if path.is_file())
        self.assertIn("example.com", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertNotRegex(text, r"[A-Za-z0-9._-]+@(?!example\.com)[a-z0-9-]+\.(com|org|net|io)\b")


if __name__ == "__main__":
    unittest.main()
