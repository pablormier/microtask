from __future__ import annotations

import json
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
SHARED_SCRIPT = SOURCE_ROOT / "deploy" / "add-shared.sh"
UPGRADE = SOURCE_ROOT / "deploy" / "upgrade.sh"
BASH = shutil.which("bash")


def clone_working_tree(destination: Path) -> None:
    """Clone this checkout, then overlay its uncommitted files so the scripts run the code under test."""
    subprocess.run(["git", "clone", "--quiet", str(SOURCE_ROOT), str(destination)],
                   check=True, capture_output=True, text=True)
    listed = subprocess.run(["git", "-C", str(SOURCE_ROOT), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], check=True, capture_output=True).stdout
    wanted = {name for name in listed.decode().split("\0") if name and (SOURCE_ROOT / name).is_file()}
    for name in wanted:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE_ROOT / name, target)
    tracked = subprocess.run(["git", "-C", str(destination), "ls-files", "-z"],
                             check=True, capture_output=True).stdout.decode().split("\0")
    for name in tracked:
        if name and name not in wanted and (destination / name).exists():
            (destination / name).unlink()
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", "-C", str(destination), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(destination), *identity, "commit", "--quiet", "--allow-empty",
                    "-m", "working tree"], check=True, capture_output=True)


def tag_releases(source: Path) -> None:
    """Give a local clone two release tags, the second on a new commit."""
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
    for command in (
        ["tag", "v1.0.0"],
        [*identity, "commit", "--quiet", "--allow-empty", "-m", "second release"],
        ["tag", "v1.1.0"],
        ["tag", "v1.3.0"],
        ["tag", "v3.0.0"],
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
        clone_working_tree(cls.source)
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
        policy = (workspace / "settings.yaml").read_text()
        self.assertIn("auto_sync: false", policy)

        self.assertEqual((self.root / "etc/mt/alice.env").read_text(),
                         "PORT=8101\nHOST_NAME=alice.mt.example.com\nMT_PROXY_SECRET_FILE=/etc/mt/alice.secret\n"
                         "MT_SIGN_OUT_URL=/oauth2/sign_out\n")
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
                         "PORT=8102\nHOST_NAME=bob.mt.example.com\nMT_PROXY_SECRET_FILE=/etc/mt/bob.secret\n"
                         "MT_SIGN_OUT_URL=/oauth2/sign_out\n")
        self.assertNotEqual((self.root / "etc/mt/bob.secret").read_text(),
                            (self.root / "etc/mt/alice.secret").read_text())
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
        self.assertIn("MT_SIGN_OUT_URL=/oauth2/sign_out", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_the_personal_dashboard_gets_a_secret_that_only_caddy_and_the_dashboard_know(self) -> None:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        result = subprocess.run(
            [BASH, "-c", 'umask 022; exec "$@"', "bash", BASH, str(SCRIPT), "--root", str(self.root),
             "--no-systemctl", "--app", str(self.app), "alice", "alice@example.com", "alice.mt.example.com"],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        secret_file = self.root / "etc/mt/alice.secret"
        secret = secret_file.read_text().strip()
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        caddy = self.root / "etc/caddy/mt-users/alice.caddy"
        self.assertEqual(stat.S_IMODE(caddy.stat().st_mode) & ~0o640, 0)
        site = caddy.read_text()
        self.assertIn(f'request_header X-Microtask-Proxy-Secret "{secret}"', site)
        self.assertIn("request_header -X-Microtask-Proxy-Secret", site)
        self.assertLess(site.index("request_header -X-Microtask-Proxy-Secret"),
                        site.index(f'request_header X-Microtask-Proxy-Secret "{secret}"'))
        self.assertNotIn("@SECRET@", site)
        self.assertIn("MT_PROXY_SECRET_FILE=/etc/mt/alice.secret", (self.root / "etc/mt/alice.env").read_text())
        # The unit passes the flag, so the dashboard refuses a request without the secret.
        unit = (SOURCE_ROOT / "deploy/systemd/mt-web@.service").read_text()
        self.assertIn("--require-proxy-secret", unit)
        self.assertNotIn("--trust-proxy-user", unit)

    def test_every_workspace_runs_as_its_own_account(self) -> None:
        for name, flag in (("mt-web@.service", "--require-proxy-secret"), ("mt-shared@.service", "--trust-proxy-user")):
            unit = (SOURCE_ROOT / "deploy/systemd" / name).read_text()
            with self.subTest(name):
                self.assertIn("\nUser=mt-%i\n", unit)
                self.assertIn("\nGroup=mt-%i\n", unit)
                self.assertNotRegex(unit, r"(?m)^(User|Group)=mt$")
                self.assertIn(flag, unit)
                for sandbox in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict",
                                "TemporaryFileSystem=/srv/mt", "BindPaths=/srv/mt/%i", "ReadWritePaths=/srv/mt/%i"):
                    self.assertIn(sandbox, unit)
        for script in (SCRIPT, SHARED_SCRIPT, UPGRADE):
            text = script.read_text()
            self.assertNotIn("MT_SERVICE_USER", text)
            self.assertNotIn("service_user", text)

    def test_names_that_would_make_an_account_name_over_32_characters_are_refused(self) -> None:
        longest = "a" * 29
        too_long = "a" * 30
        for script, arguments in ((SCRIPT, lambda n: (n, f"{n}@example.com", "x.mt.example.com")),
                                  (SHARED_SCRIPT, lambda n: (n, "x.mt.example.com", "bob@example.com"))):
            with self.subTest(script=script.name):
                refused = subprocess.run(
                    [BASH, str(script), "--root", str(self.root), "--no-systemctl", "--app", str(self.app),
                     *arguments(too_long)], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("mt-" + too_long, refused.stderr)
                self.assertIn("32", refused.stderr)
                self.assertEqual(list(self.root.iterdir()), [])
                allowed = subprocess.run(
                    [BASH, str(script), "--root", str(self.root), "--no-systemctl", "--dry-run", "--app", str(self.app),
                     *arguments(longest)], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def fake_commands(self, **failing: str) -> tuple[dict[str, str], Path]:
        """An environment whose id, useradd, userdel, chown, chgrp, getent, systemctl, caddy and runuser only log.

        A command named in `failing` exits 1 when its first argument is the given word. Returns the
        environment and the log file, one line per call.
        """
        fake = Path(tempfile.mkdtemp(dir=self.base))
        accounts = Path(tempfile.mkdtemp(dir=self.base))
        log = fake / "calls.log"
        log.write_text("")
        scripts = {
            "id": 'test -e "$FAKE_ACCOUNTS/$1"',
            "useradd": 'echo "useradd $*" >> "$FAKE_LOG"; for last; do :; done; touch "$FAKE_ACCOUNTS/$last"',
            "userdel": 'echo "userdel $*" >> "$FAKE_LOG"; rm -f "$FAKE_ACCOUNTS/$1"',
            "chown": 'echo "chown $*" >> "$FAKE_LOG"',
            "chgrp": 'echo "chgrp $*" >> "$FAKE_LOG"',
            "getent": "exit 1",
            "caddy": "exit 0",
            "systemctl": 'echo "systemctl $*" >> "$FAKE_LOG"; test "$1" != "$FAKE_FAIL_SYSTEMCTL"',
            "runuser": 'echo "runuser $*" >> "$FAKE_LOG"; shift 3; exec "$@"',
        }
        for name, body in scripts.items():
            path = fake / name
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o755)
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env.update(HOME=str(self.root), PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(log),
                   FAKE_ACCOUNTS=str(accounts), FAKE_FAIL_SYSTEMCTL=failing.get("systemctl", "never"))
        env.pop("OAUTH2_EMAILS_FILE", None)
        return env, log

    def run_with_accounts(self, script: Path, env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([BASH, str(script), "--root", str(self.root), "--app", str(self.app), *arguments],
                              check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)

    def test_each_workspace_gets_a_system_account_that_owns_its_files(self) -> None:
        env, log = self.fake_commands()
        cases = (
            (SCRIPT, ("alice", "alice@example.com", "alice.mt.example.com"), "alice", "mt-web@alice"),
            (SHARED_SCRIPT, ("team", "team.mt.example.com", "bob@example.com"), "team", "mt-shared@team"),
        )
        for script, arguments, name, unit in cases:
            with self.subTest(script=script.name):
                result = self.run_with_accounts(script, env, *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text().splitlines()
        workspace = self.root / "srv/mt"
        for name, unit in (("alice", "mt-web@alice"), ("team", "mt-shared@team")):
            self.assertIn(f"useradd --system --user-group --no-create-home --home-dir {workspace / name} "
                          f"--shell /usr/sbin/nologin mt-{name}", calls)
            self.assertIn(f"chown -R mt-{name}:mt-{name} {workspace / name}", calls)
            self.assertIn(f"chown mt-{name}:mt-{name} {self.root / 'etc/mt' / (name + '.secret')}", calls)
            if name == "team":
                self.assertIn(f"chown root:mt-{name} {self.root / 'etc/mt' / (name + '.members')}", calls)
                self.assertIn(f"chown root:mt-{name} {self.root / 'etc/mt' / (name + '.tokens.json')}", calls)
            self.assertIn(f"systemctl enable --now {unit}", calls)
            self.assertLess(calls.index(f"useradd --system --user-group --no-create-home --home-dir "
                                        f"{workspace / name} --shell /usr/sbin/nologin mt-{name}"),
                            calls.index(f"chown -R mt-{name}:mt-{name} {workspace / name}"))
        # No call names a shared account, and each workspace's files go to its own account only.
        self.assertFalse([call for call in calls if call.startswith("chown") and " mt:mt" in call])
        self.assertFalse([call for call in calls if "mt-team" in call and "alice" in call])
        # A second run for an existing account is refused before anything changes.
        again = self.run_with_accounts(SCRIPT, env, "alice", "alice@example.com", "alice.mt.example.com")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stderr)

    def test_an_existing_account_with_the_workspace_name_is_refused(self) -> None:
        env, log = self.fake_commands()
        Path(env["FAKE_ACCOUNTS"], "mt-alice").touch()
        result = self.run_with_accounts(SCRIPT, env, "alice", "alice@example.com", "alice.mt.example.com")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mt-alice already exists", result.stderr)
        result = self.run_with_accounts(SHARED_SCRIPT, env, "alice", "alice.mt.example.com", "bob@example.com")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mt-alice already exists", result.stderr)
        self.assertEqual(log.read_text(), "")
        self.assertFalse((self.root / "srv").exists())

    def test_dry_runs_mention_the_account_and_create_nothing(self) -> None:
        env, log = self.fake_commands()
        for script, arguments, account in (
            (SCRIPT, ("alice", "alice@example.com", "alice.mt.example.com"), "mt-alice"),
            (SHARED_SCRIPT, ("team", "team.mt.example.com", "bob@example.com"), "mt-team"),
        ):
            with self.subTest(script=script.name):
                result = self.run_with_accounts(script, env, "--dry-run", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"would create the system account {account}", result.stdout)
        self.assertEqual(log.read_text(), "")
        self.assertFalse((self.root / "srv").exists())
        skipped = self.add_user("--dry-run", "alice", "alice@example.com", "alice.mt.example.com")
        self.assertNotIn("system account", skipped.stdout)

    def test_a_failed_setup_removes_the_account_with_the_files(self) -> None:
        for script, arguments, name in (
            (SCRIPT, ("alice", "alice@example.com", "alice.mt.example.com"), "alice"),
            (SHARED_SCRIPT, ("team", "team.mt.example.com", "bob@example.com"), "team"),
        ):
            env, log = self.fake_commands(systemctl="enable")
            with self.subTest(script=script.name):
                result = self.run_with_accounts(script, env, *arguments)
                self.assertNotEqual(result.returncode, 0)
                calls = log.read_text().splitlines()
                self.assertTrue(any(call.startswith(f"useradd ") and call.endswith(f"mt-{name}") for call in calls))
                self.assertIn(f"userdel mt-{name}", calls)
                self.assertFalse(Path(env["FAKE_ACCOUNTS"], f"mt-{name}").exists())
                self.assertFalse((self.root / "srv/mt" / name).exists())
                self.assertFalse((self.root / "etc/mt" / f"{name}.secret").exists())
            shutil.rmtree(self.root / "etc", ignore_errors=True)
            shutil.rmtree(self.root / "srv", ignore_errors=True)

    def add_shared(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return subprocess.run(
            [BASH, str(SHARED_SCRIPT), "--root", str(self.root), "--no-systemctl",
             "--app", str(self.app), *arguments],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

    def test_shared_workspace_provisions_without_git_on_path(self) -> None:
        no_git_bin = self.root / "no-git-bin"
        no_git_bin.mkdir()
        for folder in os.environ.get("PATH", "").split(os.pathsep):
            src_dir = Path(folder)
            if not src_dir.is_dir():
                continue
            for entry in src_dir.iterdir():
                if entry.name == "git":
                    continue
                target = no_git_bin / entry.name
                if not target.exists() and os.access(entry, os.X_OK):
                    try:
                        target.symlink_to(entry)
                    except OSError:
                        pass
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env["PATH"] = str(no_git_bin)
        env.pop("OAUTH2_EMAILS_FILE", None)
        result = subprocess.run(
            [BASH, str(SHARED_SCRIPT), "--root", str(self.root), "--no-systemctl",
             "--app", str(self.app), "--admin", "alice", "team", "team.mt.example.com", "alice@example.com"],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.root / "srv" / "mt" / "team"
        self.assertTrue((workspace / "settings.yaml").is_file())

    def test_shared_workspace_creation_and_trusts_only_the_proxy(self) -> None:
        result = self.add_shared("--admin", "alice", "team", "team.mt.example.com",
                              "Alice@Example.com", "bob.smith+x@example.com")
        self.assertNotEqual(result.returncode, 0)  # a plus sign cannot be part of a user name
        self.assertFalse((self.root / "srv" / "mt" / "team").exists())

        result = self.add_shared("--admin", "alice", "team", "team.mt.example.com",
                              "Alice@Example.com", "bob@example.com", "carol@example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        workspace = self.root / "srv" / "mt" / "team"
        self.assertTrue((workspace / "settings.yaml").is_file())
        log = subprocess.run(["git", "-C", str(workspace), "log", "--oneline"],
                             check=True, capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(log), 1)
        status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain"],
                                check=True, capture_output=True, text=True).stdout
        self.assertEqual(status, "")
        validate = subprocess.run([str(self.app / "mt"), "-R", str(workspace), "validate"], check=False,
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  env={key: value for key, value in os.environ.items() if not key.startswith("MT_")})
        self.assertEqual(validate.returncode, 0, validate.stderr)

        secret_file = self.root / "etc/mt/team.secret"
        secret = secret_file.read_text().strip()
        self.assertGreaterEqual(len(secret), 32)
        self.assertEqual(stat.S_IMODE(secret_file.stat().st_mode), 0o600)
        self.assertEqual((self.root / "etc/mt/team.env").read_text(),
                         "PORT=8101\nHOST_NAME=team.mt.example.com\nMT_ADMINS=alice\n"
                         "MT_PROXY_SECRET_FILE=/etc/mt/team.secret\n"
                         "MT_MEMBERS_FILE=/etc/mt/team.members\n"
                         "MT_API_TOKENS_FILE=/etc/mt/team.tokens.json\n"
                         "MT_SIGN_OUT_URL=/oauth2/sign_out\n")
        site = (self.root / "etc/caddy/mt-users/team.caddy").read_text()
        self.assertIn("team.mt.example.com {", site)
        self.assertIn("request_header -X-Auth-Request-Email", site)
        self.assertIn("request_header -X-Microtask-Proxy-Secret", site)
        self.assertIn(f'request_header X-Microtask-Proxy-Secret "{secret}"', site)
        self.assertIn(r"`(?i)^(alice@example\.com|bob@example\.com|carol@example\.com)$`", site)
        self.assertIn("reverse_proxy 127.0.0.1:8101", site)
        self.assertNotRegex(site, r"@[A-Z_]+@")
        self.assertEqual((self.root / "etc/oauth2-proxy/emails.txt").read_text(),
                         "alice@example.com\nbob@example.com\ncarol@example.com\n")
        # The same port numbers are shared with the one-person sites.
        self.assertEqual(self.add_user("dave", "dave@example.com", "dave.mt.example.com").returncode, 0)
        self.assertIn("PORT=8102", (self.root / "etc/mt/dave.env").read_text())

    def test_shared_refusals_change_nothing(self) -> None:
        for arguments in (
            ("--admin", "zed", "team", "team.mt.example.com", "alice@example.com"),
            ("team", "team.mt.example.com", "alice@example.com", "alice@other.example.com"),
            ("team", "team.mt.example.com", "not-an-email"),
            ("Bad Name", "team.mt.example.com", "alice@example.com"),
            ("team", "nohost", "alice@example.com"),
            ("team", "team.mt.example.com"),
        ):
            with self.subTest(arguments):
                self.assertNotEqual(self.add_shared(*arguments).returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])
        dry = self.add_shared("--dry-run", "team", "team.mt.example.com", "alice@example.com")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.add_shared("team", "team.mt.example.com", "alice@example.com").returncode, 0)
        again = self.add_shared("team", "team2.mt.example.com", "bob@example.com")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("already exists", again.stderr)

    def create_team(self) -> None:
        created = self.add_shared("--admin", "alice", "team", "team.mt.example.com",
                                  "alice@example.com", "bob@example.com")
        self.assertEqual(created.returncode, 0, created.stderr)

    def test_shared_members_are_kept_in_a_private_list(self) -> None:
        self.create_team()
        members = self.root / "etc/mt/team.members"
        self.assertEqual(members.read_text(), "alice@example.com\nbob@example.com\n")
        self.assertEqual(stat.S_IMODE(members.stat().st_mode), 0o640)

    def test_shared_members_can_be_added_and_removed(self) -> None:
        self.create_team()
        caddy = self.root / "etc/caddy/mt-users/team.caddy"
        emails = self.root / "etc/oauth2-proxy/emails.txt"
        before = caddy.read_text()
        secret = (self.root / "etc/mt/team.secret").read_text().strip()

        added = self.add_shared("--add", "team", "Carol@Example.com", "bob@example.com")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual((self.root / "etc/mt/team.members").read_text(),
                         "alice@example.com\nbob@example.com\ncarol@example.com\n")
        site = caddy.read_text()
        self.assertIn(r"`(?i)^(alice@example\.com|bob@example\.com|carol@example\.com)$`", site)
        self.assertIn(f'request_header X-Microtask-Proxy-Secret "{secret}"', site)
        self.assertIn("reverse_proxy 127.0.0.1:8101", site)
        self.assertEqual(site.replace("|carol@example\\.com", ""), before)
        self.assertEqual(emails.read_text(), "alice@example.com\nbob@example.com\ncarol@example.com\n")
        self.assertIn("Nothing to add", self.add_shared("--add", "team", "carol@example.com").stdout)

        # Another site keeps an address that this one gives up.
        self.assertEqual(self.add_user("dave", "carol@example.com", "dave.mt.example.com").returncode, 0)
        removed = self.add_shared("--remove", "team", "carol@example.com", "bob@example.com")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual((self.root / "etc/mt/team.members").read_text(), "alice@example.com\n")
        self.assertIn(r"`(?i)^(alice@example\.com)$`", caddy.read_text())
        self.assertEqual(emails.read_text(), "alice@example.com\ncarol@example.com\n")

    def test_shared_tokens_file_and_mt_token_wrapper_with_auto_revoke(self) -> None:
        self.create_team()
        tokens_file = self.root / "etc/mt/team.tokens.json"
        self.assertTrue(tokens_file.is_file())
        self.assertEqual(stat.S_IMODE(tokens_file.stat().st_mode), 0o640)

        mt_token = SOURCE_ROOT / "deploy" / "mt-token"
        issued = subprocess.run(
            [str(mt_token), "--root", str(self.root), "--app", str(SOURCE_ROOT),
             "team", "issue", "bob@example.com", "--label", "ci", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        payload = json.loads(issued.stdout)
        self.assertTrue(payload["token"].startswith("mt_"))

        listed = subprocess.run(
            [str(mt_token), "--root", str(self.root), "--app", str(SOURCE_ROOT), "team", "list", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        entries = json.loads(listed.stdout)["tokens"]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["revoked_at"])

        # Removing bob from the workspace automatically revokes bob's active tokens.
        removed = self.add_shared("--remove", "team", "bob@example.com")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        listed_after = subprocess.run(
            [str(mt_token), "--root", str(self.root), "--app", str(SOURCE_ROOT), "team", "list", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(listed_after.returncode, 0, listed_after.stderr)
        self.assertIsNotNone(json.loads(listed_after.stdout)["tokens"][0]["revoked_at"])

        # Reject invalid workspace names and missing members files (fail closed).
        bad_ws = subprocess.run(
            [str(mt_token), "--root", str(self.root), "--app", str(SOURCE_ROOT), "../team", "list"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(bad_ws.returncode, 2)
        missing_ws = subprocess.run(
            [str(mt_token), "--root", str(self.root), "--app", str(SOURCE_ROOT), "taem", "issue", "alice@example.com"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(missing_ws.returncode, 1)
        self.assertFalse((self.root / "etc/mt/taem.tokens.json").exists())

        # Pre-v1 workspace without tokens.json yet still gets 0640 permissions on first issue.
        tokens_file.unlink()
        reissued = subprocess.run(
            [str(mt_token), "--root", str(self.root), "--app", str(SOURCE_ROOT),
             "team", "issue", "alice@example.com", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(reissued.returncode, 0, reissued.stderr)
        self.assertEqual(stat.S_IMODE(tokens_file.stat().st_mode), 0o640)

    def test_shared_member_changes_are_checked_and_can_be_previewed(self) -> None:
        self.create_team()
        snapshot = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for arguments in (
            ("--add", "team", "alice@other.example.com"),     # same user name as alice
            ("--add", "team", "not-an-email"),
            ("--add", "nosuch", "carol@example.com"),
            ("--add", "--admin", "carol", "team", "carol@example.com"),
            ("--remove", "team", "carol@example.com"),        # not a member
            ("--remove", "team", "alice@example.com"),        # an administrator
            ("--remove", "team", "alice@example.com", "bob@example.com"),  # nobody left
        ):
            with self.subTest(arguments):
                self.assertNotEqual(self.add_shared(*arguments).returncode, 0)
        self.assertIn("MT_ADMINS", self.add_shared("--remove", "team", "alice@example.com").stderr)
        for arguments in (("--add", "team", "carol@example.com"), ("--remove", "team", "bob@example.com")):
            dry = self.add_shared("--dry-run", *arguments)
            self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, snapshot)
        # Once the administrator list no longer names the person, removal works.
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text().replace("MT_ADMINS=alice", "MT_ADMINS="))
        self.assertEqual(self.add_shared("--remove", "team", "alice@example.com").returncode, 0)

    def add_shared_with_umask(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        env.pop("OAUTH2_EMAILS_FILE", None)
        return subprocess.run(
            [BASH, "-c", 'umask 022; exec "$@"', "bash", BASH, str(SHARED_SCRIPT), "--root", str(self.root),
             "--no-systemctl", "--app", str(self.app), *arguments],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        )

    def assert_private_files(self) -> None:
        for name, limit in (("caddy/mt-users/team.caddy", 0o640), ("mt/team.secret", 0o600),
                            ("mt/team.members", 0o640), ("mt/team.tokens.json", 0o640)):
            path = self.root / "etc" / name
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & ~limit, 0, f"{name} is {oct(mode)}, more open than {oct(limit)}")
            self.assertEqual(mode & 0o007, 0, f"{name} is readable by others")
        retired = self.root / "etc/mt/team.retired"
        if retired.exists():
            self.assertEqual(stat.S_IMODE(retired.stat().st_mode) & 0o077, 0)

    def test_shared_files_with_the_secret_are_never_open_to_others_under_a_permissive_umask(self) -> None:
        created = self.add_shared_with_umask("--admin", "alice", "team", "team.mt.example.com",
                                             "alice@example.com", "bob@example.com")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assert_private_files()
        for arguments in (("--add", "team", "carol@example.com"), ("--remove", "team", "bob@example.com")):
            result = self.add_shared_with_umask(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_private_files()
        # A site file left open by an older version is closed again by the next update.
        caddy = self.root / "etc/caddy/mt-users/team.caddy"
        caddy.chmod(0o644)
        self.assertEqual(self.add_shared_with_umask("--add", "team", "dave@example.com").returncode, 0)
        self.assert_private_files()

    def start_waiting(self, script: Path, *arguments: str) -> tuple[Path, subprocess.Popen]:
        """Start a script that waits for the shared lock, which this test holds until it releases it."""
        lock = self.root / "etc/mt/.add-user.lock"
        lock.mkdir(parents=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        process = subprocess.Popen(
            [BASH, str(script), "--root", str(self.root), "--no-systemctl", "--app", str(self.app), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL)
        time.sleep(0.5)
        return lock, process

    def make_shared_style_files(self) -> None:
        (self.root / "srv/mt/team").mkdir(parents=True)
        (self.root / "srv/mt/team/keep.txt").write_text("first run")
        for name, text in (("team.env", "PORT=8101\n"), ("team.secret", "s\n"), ("team.members", "a@example.com\n")):
            (self.root / "etc/mt" / name).write_text(text)
        (self.root / "etc/caddy/mt-users").mkdir(parents=True)
        (self.root / "etc/caddy/mt-users/team.caddy").write_text("site\n")

    def test_add_user_that_loses_the_race_to_add_shared_leaves_the_shared_workspace_alone(self) -> None:
        lock, process = self.start_waiting(SCRIPT, "team", "carol@example.com", "carol.mt.example.com")
        self.make_shared_style_files()
        lock.rmdir()
        _, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("already exists", stderr)
        self.assertEqual((self.root / "srv/mt/team/keep.txt").read_text(), "first run")
        self.assertEqual((self.root / "etc/mt/team.env").read_text(), "PORT=8101\n")
        self.assertEqual((self.root / "etc/mt/team.secret").read_text(), "s\n")
        self.assertEqual((self.root / "etc/caddy/mt-users/team.caddy").read_text(), "site\n")
        self.assertFalse(lock.exists())

    def test_add_shared_that_loses_the_race_to_add_user_leaves_the_user_alone(self) -> None:
        lock, process = self.start_waiting(SHARED_SCRIPT, "team", "team.mt.example.com", "bob@example.com")
        (self.root / "srv/mt/team").mkdir(parents=True)
        (self.root / "srv/mt/team/keep.txt").write_text("first run")
        (self.root / "etc/mt/team.env").write_text("PORT=8101\nHOST_NAME=x.example.com\n")
        (self.root / "etc/caddy/mt-users").mkdir(parents=True)
        (self.root / "etc/caddy/mt-users/team.caddy").write_text("site\n")
        lock.rmdir()
        _, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("already exists", stderr)
        self.assertEqual((self.root / "srv/mt/team/keep.txt").read_text(), "first run")
        self.assertEqual((self.root / "etc/mt/team.env").read_text(), "PORT=8101\nHOST_NAME=x.example.com\n")
        self.assertEqual((self.root / "etc/caddy/mt-users/team.caddy").read_text(), "site\n")
        self.assertFalse((self.root / "etc/mt/team.secret").exists())

    def test_add_user_refuses_a_name_that_only_has_shared_files(self) -> None:
        for leftover in ("team.secret", "team.members", "team.retired"):
            with self.subTest(leftover):
                (self.root / "etc/mt").mkdir(parents=True, exist_ok=True)
                path = self.root / "etc/mt" / leftover
                path.write_text("x\n")
                result = self.add_user("team", "carol@example.com", "carol.mt.example.com")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already exists", result.stderr)
                self.assertEqual(path.read_text(), "x\n")
                self.assertFalse((self.root / "srv/mt/team").exists())
                path.unlink()

    def set_user_map(self, text: str, admins: str | None = None) -> None:
        user_map = self.root / "etc/mt/team.users"
        user_map.write_text(text)
        env_file = self.root / "etc/mt/team.env"
        content = env_file.read_text()
        if admins is not None:
            content = content.replace("MT_ADMINS=alice", f"MT_ADMINS={admins}")
        env_file.write_text(content + f"MT_USER_MAP={user_map}\n")

    def test_member_user_names_are_the_ones_the_dashboard_uses(self) -> None:
        self.create_team()
        # Different local parts, same mapped name.
        self.set_user_map("carol@example.com bob\n")
        refused = self.add_shared("--add", "team", "carol@example.com")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("user name bob", refused.stderr)
        self.assertNotIn("carol@example.com", (self.root / "etc/mt/team.members").read_text())
        # The same local part is fine when the map gives the newcomer another name.
        self.set_user_map("bob@other.example.org bob2\n")
        self.assertEqual(self.add_shared("--add", "team", "bob@other.example.org").returncode, 0)
        self.assertIn("bob@other.example.org", (self.root / "etc/mt/team.members").read_text())
        # A mapped name must be a valid user name.
        self.set_user_map("dave@example.com Bad_Name\n")
        self.assertNotEqual(self.add_shared("--add", "team", "dave@example.com").returncode, 0)

    def test_the_administrator_check_uses_the_mapped_name(self) -> None:
        self.create_team()
        # alice is mapped to boss, and boss administers: removing her is refused.
        self.set_user_map("alice@example.com boss\n", admins="boss")
        refused = self.add_shared("--remove", "team", "alice@example.com")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("MT_ADMINS", refused.stderr)
        # Her local part does not make her an administrator: she is boss to the dashboard.
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text().replace("MT_ADMINS=boss", "MT_ADMINS=bob"))
        self.assertEqual(self.add_shared("--remove", "team", "alice@example.com").returncode, 0)

    def test_member_changes_stop_when_the_configured_user_map_cannot_be_used(self) -> None:
        self.create_team()
        user_map = self.root / "etc/mt/team.users"
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text() + f"MT_USER_MAP={user_map}\n")
        snapshot = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        cases = [("missing", None, "does not exist"),
                 ("one field", "carol@example.com\n", "EMAIL USER"),
                 ("three fields", "carol@example.com carol extra\n", "EMAIL USER"),
                 ("not an email", "carol carol2\n", "not an email"),
                 ("bad name", "carol@example.com Carol\n", "user name must be"),
                 ("duplicate", "carol@example.com c1\ncarol@example.com c2\n", "twice")]
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            cases.append(("unreadable", "carol@example.com c1\n", "cannot read"))
        for label, content, message in cases:
            if content is not None:
                user_map.write_text(content)
                if label == "unreadable":
                    user_map.chmod(0)
            for arguments in (("--add", "team", "carol@example.com"), ("--remove", "team", "bob@example.com")):
                for dry in ((), ("--dry-run",)):
                    with self.subTest(label=label, arguments=arguments, dry=dry):
                        result = self.add_shared(*dry, *arguments)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(message, result.stderr)
            if label == "unreadable":
                user_map.chmod(0o600)
            if user_map.exists() and label != "missing":
                snapshot[user_map] = user_map.read_bytes()
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, {**snapshot, **{p: b for p, b in after.items() if p == user_map}})
        user_map.write_text("# comment\ncarol@example.com carol2 # trailing\n")
        self.assertEqual(self.add_shared("--dry-run", "--add", "team", "carol@example.com").returncode, 0)

    def test_admin_names_are_read_like_the_dashboard(self) -> None:
        self.create_team()
        env_file = self.root / "etc/mt/team.env"
        for admins in ("alice, bob", "alice bob", "alice,bob", " alice ,, bob "):
            env_file.write_text(env_file.read_text().replace(
                next(line for line in env_file.read_text().splitlines() if line.startswith("MT_ADMINS=")),
                f"MT_ADMINS={admins}"))
            with self.subTest(admins=admins):
                refused = self.add_shared("--dry-run", "--remove", "team", "bob@example.com")
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("MT_ADMINS", refused.stderr)
                self.assertEqual(self.add_shared("--add", "team", "carol@example.com").returncode, 0)
                allowed = self.add_shared("--remove", "team", "carol@example.com")
                self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_admin_flag_values_are_written_as_a_plain_comma_list(self) -> None:
        result = self.add_shared("--admin", "alice", "--admin", "bob, carol", "team", "team.mt.example.com",
                                 "alice@example.com", "bob@example.com", "carol@example.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MT_ADMINS=alice,bob,carol\n", (self.root / "etc/mt/team.env").read_text())

    def test_a_user_map_with_windows_line_endings_does_not_block_other_changes(self) -> None:
        self.create_team()
        user_map = self.root / "etc/mt/team.users"
        user_map.write_bytes(b"# map\r\nalice@example.com alice\r\nbob@example.com bob\r\ncarol@example.com carol2\r\n")
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text() + f"MT_USER_MAP={user_map}\n")
        added = self.add_shared("--add", "team", "carol@example.com")
        self.assertEqual(added.returncode, 0, added.stderr)
        removed = self.add_shared("--remove", "team", "carol@example.com")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        retired = (self.root / "etc/mt/team.retired").read_bytes()
        self.assertEqual(retired, b"carol2 carol@example.com\n")
        # An unrelated member leaves while the map still has CRLF lines.
        removed = self.add_shared("--remove", "team", "bob@example.com")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertNotIn(b"\r", (self.root / "etc/mt/team.retired").read_bytes())
        self.assertNotIn(b"\r", (self.root / "etc/mt/team.members").read_bytes())

    def test_a_second_create_that_loses_the_race_leaves_the_first_workspace_alone(self) -> None:
        lock = self.root / "etc/mt/.add-user.lock"
        lock.mkdir(parents=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith("MT_")}
        env["HOME"] = str(self.root)
        process = subprocess.Popen(
            [BASH, str(SHARED_SCRIPT), "--root", str(self.root), "--no-systemctl", "--app", str(self.app),
             "team", "team.mt.example.com", "bob@example.com"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, stdin=subprocess.DEVNULL)
        # The second run passed its early check and now waits for the lock. The first run finishes.
        time.sleep(0.5)
        workspace = self.root / "srv/mt/team"
        workspace.mkdir(parents=True)
        (workspace / "keep.txt").write_text("first run")
        (self.root / "etc/mt/team.env").write_text("PORT=8101\n")
        lock.rmdir()
        _, stderr = process.communicate(timeout=30)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("already exists", stderr)
        self.assertEqual((workspace / "keep.txt").read_text(), "first run")
        self.assertEqual((self.root / "etc/mt/team.env").read_text(), "PORT=8101\n")
        self.assertFalse((self.root / "etc/mt/team.secret").exists())
        self.assertFalse(lock.exists())

    def test_a_removed_members_user_name_is_not_handed_to_someone_else(self) -> None:
        self.create_team()
        retired = self.root / "etc/mt/team.retired"
        self.assertEqual(self.add_shared("--remove", "team", "bob@example.com").returncode, 0)
        self.assertEqual(retired.read_text(), "bob bob@example.com\n")
        self.assertEqual(stat.S_IMODE(retired.stat().st_mode), 0o600)
        snapshot = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        refused = self.add_shared("--add", "team", "bob@other.example.org")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--reuse-name", refused.stderr)
        self.assertIn("MT_USER_MAP", refused.stderr)
        dry = self.add_shared("--dry-run", "--add", "team", "bob@other.example.org")
        self.assertNotEqual(dry.returncode, 0)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, snapshot)
        self.assertNotEqual(self.add_shared("--remove", "--reuse-name", "team", "alice@example.com").returncode, 0)

        # A different name through the user map is fine.
        user_map = self.root / "etc/mt/team.users"
        user_map.write_text("bob@other.example.org bob2\n")
        env_file = self.root / "etc/mt/team.env"
        env_file.write_text(env_file.read_text() + f"MT_USER_MAP={user_map}\n")
        self.assertEqual(self.add_shared("--add", "team", "bob@other.example.org").returncode, 0)
        self.assertEqual(retired.read_text(), "bob bob@example.com\n")

    def test_a_retired_user_name_can_come_back_with_the_same_address_or_the_reuse_flag(self) -> None:
        self.create_team()
        retired = self.root / "etc/mt/team.retired"
        members = self.root / "etc/mt/team.members"
        self.assertEqual(self.add_shared("--remove", "team", "bob@example.com").returncode, 0)
        # The same address is the same person, so the name comes back without a flag.
        self.assertEqual(self.add_shared("--add", "team", "Bob@Example.com").returncode, 0)
        self.assertEqual(retired.read_text(), "")
        self.assertIn("bob@example.com", members.read_text())
        # After another removal a different address needs the explicit flag.
        self.assertEqual(self.add_shared("--remove", "team", "bob@example.com").returncode, 0)
        self.assertEqual(self.add_shared("--add", "--reuse-name", "team", "bob@other.example.org").returncode, 0)
        self.assertEqual(retired.read_text(), "")
        self.assertIn("bob@other.example.org", members.read_text())
        # Now bob@example.com is blocked from taking the name back from the new person.
        self.assertEqual(self.add_shared("--remove", "team", "bob@other.example.org").returncode, 0)
        self.assertEqual(retired.read_text(), "bob bob@other.example.org\n")

    def test_shared_script_syntax_and_shellcheck(self) -> None:
        syntax = subprocess.run([BASH, "-n", str(SHARED_SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            self.skipTest("shellcheck is not installed")
        lint = subprocess.run([shellcheck, str(SHARED_SCRIPT)], check=False, capture_output=True, text=True)
        self.assertEqual(lint.returncode, 0, lint.stdout)

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
        clone_working_tree(cls.source)
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

    def test_each_workspace_is_checked_as_its_own_account(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        self.add_user("bob")
        fake = self.root / "fake-bin"
        fake.mkdir()
        log = fake / "calls.log"
        stubs = {
            "id": "exit 0",
            "runuser": 'echo "runuser $*" >> "$FAKE_LOG"; shift 3; exec "$@"',
            "chown": "exit 0",
            "systemctl": "exit 1",
        }
        for name, body in stubs.items():
            (fake / name).write_text(f"#!/bin/sh\n{body}\n")
            (fake / name).chmod(0o755)
        env = self.environment()
        env.update(PATH=f"{fake}{os.pathsep}{os.environ['PATH']}", FAKE_LOG=str(log))
        result = subprocess.run(
            [BASH, str(UPGRADE), "--root", str(self.root), "--source", str(self.source), "v1.1.0"],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text()
        self.assertIn("runuser -u mt-alice -- env HOME=", calls)
        self.assertIn("runuser -u mt-bob -- env HOME=", calls)
        self.assertNotIn("runuser -u mt ", calls)
        # A workspace without its account stops the switch.
        (fake / "id").write_text('#!/bin/sh\ntest "$1" != mt-bob\n')
        result = subprocess.run(
            [BASH, str(UPGRADE), "--root", str(self.root), "--source", str(self.source), "--allow-skip", "v1.3.0"],
            check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mt-bob does not exist", result.stderr)
        self.assertEqual(self.link("current"), "v1.1.0")

    def test_first_install_creates_version_and_current_only(self) -> None:
        result = self.upgrade("v1.0.0")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.apps / "v1.0.0" / "mt").is_file())
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertIsNone(self.link("previous"))
        self.assertIn("v1.0.0", result.stdout)
        self.assertTrue((self.apps / "v1.0.0" / ".mt-hosted").is_file())
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

    def test_workspace_still_using_the_old_settings_name_stops_the_switch(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)
        self.add_user("alice")
        self.add_user("bob")
        (self.root / "srv/mt/bob/settings.yaml").rename(self.root / "srv/mt/bob/mt-policy.yaml")

        result = self.upgrade("v1.1.0")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAILED: bob", result.stderr)
        self.assertIn("mt-policy.yaml", result.stderr)
        self.assertIn("migrate", result.stderr)
        self.assertNotIn("FAILED: alice", result.stderr)
        self.assertEqual(self.link("current"), "v1.0.0")
        self.assertFalse((self.root / "srv/mt/bob/settings.yaml").exists())

    def test_skipping_a_minor_version_is_refused_unless_allowed(self) -> None:
        self.assertEqual(self.upgrade("v1.0.0").returncode, 0)

        for tag in ("v1.3.0", "v3.0.0"):
            with self.subTest(tag=tag):
                result = self.upgrade(tag)
                dry = self.upgrade("--dry-run", tag)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotEqual(dry.returncode, 0)
                self.assertIn("one minor version at a time", result.stderr)
                self.assertIn("--allow-skip", result.stderr)
                self.assertFalse((self.apps / tag).exists())
                self.assertEqual(self.link("current"), "v1.0.0")

        self.assertIn("the next one is v1.1.x", self.upgrade("v1.3.0").stderr)

        allowed = self.upgrade("--allow-skip", "v1.3.0")

        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(self.link("current"), "v1.3.0")

    def test_the_next_minor_comes_from_the_tags_even_across_a_major(self) -> None:
        # Tags are v1.0.0, v1.1.0, v1.3.0 and v3.0.0: there is no v1.2.x or v2.x.
        self.assertEqual(self.upgrade("v1.1.0").returncode, 0)
        self.assertEqual(self.upgrade("v1.3.0").returncode, 0, "v1.2.x does not exist, so v1.3.0 is next")
        result = self.upgrade("v3.0.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link("current"), "v3.0.0")

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
        text = "\n".join(path.read_text() for path in (SOURCE_ROOT / "deploy").rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts)
        self.assertIn("example.com", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        self.assertNotRegex(text, r"[A-Za-z0-9._-]+@(?!example\.com)[a-z0-9-]+\.(com|org|net|io)\b")


if __name__ == "__main__":
    unittest.main()
